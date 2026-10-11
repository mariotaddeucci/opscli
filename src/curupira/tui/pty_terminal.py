"""Reusable Textual widget that hosts an interactive PTY child process.

``PtyTerminal`` is the building block for an embedded side-panel coding
assistant inside Curupira's Textual dashboard. This module does not wire the
widget into the orchestrator layout or configuration.

Platform support: Linux and macOS; Windows unsupported in v1. On Windows the
widget mounts a clear placeholder instead of raising at import time.

The child is spawned with ``pty.fork`` (session leader), driven by asyncio
``add_reader`` on the master fd, and emulated with ``pyte``. PTY bytes are never
logged. Process lifecycle cleanup runs in ``on_unmount`` (the App's teardown is
too late for reliable reaping) with an ``atexit`` safety net.

``pyte`` is LGPL-3.0 and is used as a dynamic dependency of this MIT-licensed
project. The reader feeds pyte in 256-byte slices under a 5 ms per-tick time
budget checked before and after each feed, then yields (``remove_reader`` /
``asyncio.sleep(0)`` / re-add) so the event loop stays responsive during
floods. Rendering caches Rich styles, coalesces identical adjacent cells into
Textual strips, and refreshes dirty rows at about 30 fps. Measured Linux
figures live in CONTRIBUTING.md.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import errno
import fcntl
import logging
import os
import signal
import struct
import sys
import time
import weakref
from collections import deque
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import ClassVar

import pyte
from pyte import modes as pyte_modes
from pyte.screens import Char, Margins
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import RenderResult
from textual.binding import BindingType
from textual.geometry import Region
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.widget import Widget
from typing_extensions import override

from curupira.tui.pty_keys import bracketed_paste_enabled, key_to_bytes, paste_to_bytes
from curupira.tui.pty_render import render_line_strip, render_line_text

logger = logging.getLogger(__name__)

_DEFAULT_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "LANG", "USER", "SHELL")
_DEFAULT_SCROLLBACK = 5_000
_DEFAULT_ORPHAN_TIMEOUT_SECONDS = 30.0
_REFRESH_INTERVAL_SECONDS = 1.0 / 30.0
_KILL_GRACE_SECONDS = 0.1
# Keep each feed tiny and check the time budget before and after every slice so
# a single reader callback cannot stall the event loop.
_READ_CHUNK_BYTES = 1_024
_FEED_SLICE_BYTES = 256
_FEED_TIME_BUDGET_SECONDS = 0.005
_POLL_INTERVAL_SECONDS = 0.05
_MAX_WRITE_BUFFER_BYTES = 1_048_576
_UNSUPPORTED_MESSAGE = "PTY terminals are not supported on Windows in v1."
_LIVE_TERMINALS: weakref.WeakSet[PtyTerminal] = weakref.WeakSet()
_ATEXIT_REGISTERED = False
_CHILD_RESET_SIGNALS: tuple[signal.Signals, ...] = (
    signal.SIGPIPE,
    signal.SIGINT,
    signal.SIGQUIT,
)


def default_pty_environment() -> dict[str, str]:
    """Build the default allowlisted environment for a PTY child.

    Returns:
        A copy of selected parent variables plus ``TERM`` and ``COLORTERM``.
    """
    environment = {
        key: value for key in _DEFAULT_ENV_KEYS if (value := os.environ.get(key)) is not None
    }
    environment["TERM"] = "xterm-256color"
    environment["COLORTERM"] = "truecolor"
    return environment


def clamp_terminal_dimensions(width: int, height: int) -> tuple[int, int]:
    """Clamp PTY columns/rows to at least 1x1 (including a zero content size)."""
    return max(1, width), max(1, height)


def _posix_supported() -> bool:
    """Return whether this interpreter can host a PTY child."""
    return os.name == "posix" and sys.platform != "win32"


def _atexit_cleanup() -> None:
    """Best-effort reaping if the process exits without unmounting widgets."""
    for terminal in list(_LIVE_TERMINALS):
        terminal._shutdown_child(grace_seconds=0.0)


def _ensure_atexit() -> None:
    """Register the process-wide PTY cleanup hook once."""
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    atexit.register(_atexit_cleanup)
    _ATEXIT_REGISTERED = True


def _reset_child_signals() -> None:
    """Restore signals Python ignores so pipeline children receive SIGPIPE."""
    for sig in _CHILD_RESET_SIGNALS:
        with contextlib.suppress(OSError, ValueError, AttributeError):
            signal.signal(sig, signal.SIG_DFL)


class _EmulatorScreen(pyte.Screen):
    """pyte screen with cheap scrollback that replies to device-status queries.

    ``pyte.HistoryScreen`` wraps every stream event through ``__getattribute__``,
    which makes dense newline floods several times slower than ``Screen``. This
    subclass keeps a bounded deque of scrolled-off row buffers without that
    per-event wrapper cost.
    """

    def __init__(
        self,
        columns: int,
        lines: int,
        *,
        history: int,
        on_write: Callable[[bytes], None],
    ) -> None:
        # ``Screen.__init__`` calls ``reset()``; allocate scrollback first.
        self.scrollback: deque[MutableMapping[int, Char]] = deque(maxlen=max(history, 0))
        self._on_write = on_write
        super().__init__(columns, lines)

    @override
    def write_process_input(self, data: str) -> None:
        """Forward DA/DSR replies to the PTY master."""
        self._on_write(data.encode("utf-8", errors="replace"))

    @override
    def index(self) -> None:
        """Scroll up and retain the discarded top row in ``scrollback``."""
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == bottom and self.scrollback.maxlen:
            self.scrollback.append(self.buffer[top])
        super().index()

    @override
    def reset(self) -> None:
        """Reset the screen and drop retained scrollback rows."""
        super().reset()
        self.scrollback.clear()

    @override
    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        """Resize the screen and drop scrollback that no longer matches geometry."""
        super().resize(lines=lines, columns=columns)
        self.scrollback.clear()


class PtyTerminal(Widget, can_focus=True):
    """Focusable Textual widget hosting one interactive PTY child.

    Platform support: Linux and macOS; Windows unsupported in v1.

    Attributes:
        argv: Argument vector for the child (``argv[0]`` is the executable).
        env: Explicit environment allowlist for the child. When omitted, Curupira
            copies ``PATH``, ``HOME``, ``LANG``, ``USER``, and ``SHELL`` from the
            parent and sets ``TERM=xterm-256color`` plus ``COLORTERM=truecolor``.
        cwd: Optional working directory for the child.
        escape_key: Priority binding that releases focus instead of forwarding
            the chord to the child (default ``ctrl+g``).
    """

    DEFAULT_CSS = """
    PtyTerminal {
        background: #000000;
        color: #e0e0e0;
        overflow: hidden;
    }
    PtyTerminal:focus {
        border: tall #c8c8c8;
    }
    PtyTerminal.-unsupported {
        content-align: center middle;
        color: #ff5555;
        text-style: bold;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = []

    unsupported: reactive[bool] = reactive(False)
    finished_code: reactive[int | None] = reactive(None)

    class Finished(Message):
        """Posted when the PTY child exits.

        Attributes:
            exit_code: Process exit status, or ``-1`` when unavailable.
        """

        def __init__(self, exit_code: int) -> None:
            super().__init__()
            self.exit_code = exit_code

    class FocusReleased(Message):
        """Posted when the configured escape key releases focus."""

    def __init__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        *,
        escape_key: str = "ctrl+g",
        scrollback: int = _DEFAULT_SCROLLBACK,
        orphan_timeout_seconds: float = _DEFAULT_ORPHAN_TIMEOUT_SECONDS,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Create a PTY terminal widget.

        Args:
            argv: Child argument vector; must be non-empty.
            env: Explicit environment mapping (allowlist). ``None`` selects the
                default allowlist from :func:`default_pty_environment`.
            cwd: Working directory for the child, or ``None`` for the parent cwd.
            escape_key: Key binding that returns focus to the host UI.
            scrollback: Bounded pyte history lines retained above the viewport.
            orphan_timeout_seconds: After the PTY master closes, wait this long
                for a natural child exit before escalating to ``SIGHUP`` /
                ``SIGKILL``. Unmount, restart, and atexit always escalate.
            name: Optional Textual widget name.
            id: Optional Textual widget id.
            classes: Optional Textual CSS classes.
        """
        if not argv:
            raise ValueError("argv must contain at least the executable path")
        if orphan_timeout_seconds < 0:
            raise ValueError("orphan_timeout_seconds must be non-negative")
        super().__init__(name=name, id=id, classes=classes)
        self._argv = tuple(argv)
        self._env = dict(env) if env is not None else default_pty_environment()
        self._env.setdefault("TERM", "xterm-256color")
        self._env.setdefault("COLORTERM", "truecolor")
        self._cwd = Path(cwd) if cwd is not None else None
        self._escape_key = escape_key
        self._scrollback = scrollback
        self._orphan_timeout_seconds = orphan_timeout_seconds
        self._supported = _posix_supported()
        self.unsupported = not self._supported
        self._master_fd: int | None = None
        self._pid: int | None = None
        self._emulator: _EmulatorScreen | None = None
        self._stream: pyte.ByteStream | None = None
        self._render_dirty = False
        self._line_cache: list[Text] = []
        self._strip_cache: list[Strip] = []
        self._cached_cursor_row: int | None = None
        self._reader_installed = False
        self._writer_installed = False
        self._reader_resume_task: asyncio.Task[None] | None = None
        self._pending_input = bytearray()
        self._write_buffer = bytearray()
        self._shutting_down = False
        self._reap_task: asyncio.Task[None] | None = None
        self._placeholder = Text(_UNSUPPORTED_MESSAGE)
        self._bindings.bind(
            escape_key,
            "release_focus",
            description="Release focus",
            show=False,
            priority=True,
        )

    @property
    def supported(self) -> bool:
        """Whether this platform can spawn a PTY child."""
        return self._supported

    @property
    def pid(self) -> int | None:
        """Child process id while running, otherwise ``None``."""
        return self._pid

    @override
    def render(self) -> RenderResult:
        """Render the emulated buffer, optional exit footer, or placeholder."""
        if not self._supported:
            return self._placeholder
        if self._emulator is None:
            return self._placeholder
        self._sync_render_caches(rebuild_text=True)
        output = Text()
        for row_index, line in enumerate(self._line_cache[: self._emulator.lines]):
            if row_index:
                output.append("\n")
            output.append_text(line)
        return output

    @override
    def render_line(self, y: int) -> Strip:
        """Return a cached content strip so Textual skips full Visual conversion."""
        if not self._supported or self._emulator is None:
            return super().render_line(y)
        if y < len(self._strip_cache):
            return self._strip_cache[y]
        width = max(self.content_size.width, 1)
        return Strip.blank(width, self.rich_style)

    def _exit_status_message(self) -> str | None:
        """Return the on-screen exit banner, or ``None`` while the child runs."""
        if self.finished_code is None:
            return None
        return f"Process exited ({self.finished_code})"

    def _overlay_exit_status(self, *, rebuild_text: bool) -> int | None:
        """Paint the exit banner onto the last content row; return that row index.

        The banner must live inside the content height: appending an extra line
        after the emulator buffer places it outside the visible region.
        """
        message = self._exit_status_message()
        if message is None or not self._strip_cache:
            return None
        width = max(self.content_size.width, self._emulator.columns if self._emulator else 1)
        width = max(width, 1)
        padded = message[:width].ljust(width)
        last_row = len(self._strip_cache) - 1
        self._strip_cache[last_row] = Strip([Segment(padded, Style(bold=True))], width)
        if rebuild_text and self._line_cache:
            while len(self._line_cache) <= last_row:
                self._line_cache.append(Text(" " * width))
            self._line_cache[last_row] = Text(padded, style="bold")
        return last_row

    def write(self, data: bytes | str) -> None:
        """Write bytes (or UTF-8 text) to the PTY master.

        Args:
            data: Payload forwarded to the child's stdin.
        """
        if self._master_fd is None:
            return
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self._write_master(payload)

    def restart(self) -> None:
        """Kill the current child (if any) and spawn a fresh one."""
        if not self._supported:
            return
        self._shutdown_child(grace_seconds=_KILL_GRACE_SECONDS)
        self.finished_code = None
        self._spawn()

    def action_release_focus(self) -> None:
        """Handle the escape key: blur this widget and notify the host."""
        self.blur()
        self.post_message(self.FocusReleased())

    def on_mount(self) -> None:
        """Spawn the child on POSIX hosts and start render batching."""
        if not self._supported:
            self.add_class("-unsupported")
            self._placeholder = Text(_UNSUPPORTED_MESSAGE)
            return
        _ensure_atexit()
        _LIVE_TERMINALS.add(self)
        # Spawn after the first layout pass so the child does not start at a
        # transient 1x1 content size that scrolls early output out of view.
        self.call_after_refresh(self._spawn_after_layout)
        self.set_interval(_REFRESH_INTERVAL_SECONDS, self._flush_if_dirty, name="pty-refresh")

    def on_unmount(self) -> None:
        """Close the master, signal the process group, and reap the child."""
        self._shutdown_child(grace_seconds=_KILL_GRACE_SECONDS)
        _LIVE_TERMINALS.discard(self)

    def on_resize(self, event: events.Resize) -> None:
        """Resize the emulator and notify the child with ``SIGWINCH``."""
        del event
        self._apply_winsize()

    def on_key(self, event: events.Key) -> None:
        """Forward keystrokes to the child, except the escape binding."""
        if not self._supported or self._master_fd is None:
            return
        if event.key == self._escape_key:
            return
        modes = self._emulator.mode if self._emulator is not None else set()
        payload = key_to_bytes(event, modes=modes)
        if payload is None:
            return
        event.stop()
        event.prevent_default()
        self._write_master(payload)

    def on_paste(self, event: events.Paste) -> None:
        """Forward pasted text, wrapping it when mode 2004 is active."""
        if not self._supported or self._master_fd is None or self._emulator is None:
            return
        event.stop()
        event.prevent_default()
        bracketed = bracketed_paste_enabled(self._emulator.mode)
        self._write_master(paste_to_bytes(event.text, bracketed=bracketed))

    def _spawn_after_layout(self) -> None:
        """Spawn once the widget has a laid-out content size."""
        if self._pid is not None or self._emulator is not None:
            return
        self._spawn()

    def _spawn(self) -> None:
        """Fork a PTY session and exec ``argv`` in the child."""
        import pty

        self._cancel_reap_task()
        self._shutting_down = False
        columns, lines = self._content_dimensions()
        self._emulator = _EmulatorScreen(
            columns,
            lines,
            history=self._scrollback,
            on_write=self._write_master,
        )
        self._stream = pyte.ByteStream(self._emulator)
        self._line_cache.clear()
        self._strip_cache.clear()
        self._cached_cursor_row = None
        self._render_dirty = True

        pid, master_fd = pty.fork()
        if pid == 0:
            self._child_exec()
            os._exit(127)

        self._pid = pid
        self._master_fd = master_fd
        os.set_blocking(master_fd, False)
        self._apply_winsize()
        self._install_reader()

    def _child_exec(self) -> None:
        """Run inside the forked child: reset signals/env and exec ``argv``."""
        try:
            _reset_child_signals()
            if self._cwd is not None:
                os.chdir(self._cwd)
            os.environ.clear()
            os.environ.update(self._env)
            os.execvp(self._argv[0], list(self._argv))
        except Exception:
            # Child must never unwind into the parent Textual process.
            os._exit(127)
        os._exit(127)

    def _install_reader(self) -> None:
        """Register the master fd with the asyncio event loop."""
        if self._master_fd is None or self._reader_installed:
            return
        loop = asyncio.get_running_loop()
        loop.add_reader(self._master_fd, self._on_master_readable)
        self._reader_installed = True

    def _remove_reader(self) -> None:
        """Drop the asyncio reader for the master fd."""
        if self._master_fd is None or not self._reader_installed:
            return
        with contextlib.suppress(RuntimeError, ValueError, OSError):
            asyncio.get_running_loop().remove_reader(self._master_fd)
        self._reader_installed = False

    def _install_writer(self) -> None:
        """Register a writability callback while the outbound buffer is non-empty."""
        if self._master_fd is None or self._writer_installed:
            return
        loop = asyncio.get_running_loop()
        loop.add_writer(self._master_fd, self._flush_write_buffer)
        self._writer_installed = True

    def _remove_writer(self) -> None:
        """Drop the asyncio writer for the master fd."""
        if self._master_fd is None or not self._writer_installed:
            return
        with contextlib.suppress(RuntimeError, ValueError, OSError):
            asyncio.get_running_loop().remove_writer(self._master_fd)
        self._writer_installed = False

    def _on_master_readable(self) -> None:
        """Feed pyte under a short time budget, then yield to the event loop."""
        if self._master_fd is None or self._stream is None:
            return
        deadline = time.perf_counter() + _FEED_TIME_BUDGET_SECONDS
        total = self._drain_pending_under_budget(deadline)
        if total < 0:
            return
        while time.perf_counter() < deadline:
            chunk = self._read_master_chunk()
            if chunk is None:
                break
            if chunk == b"":
                self._on_master_closed()
                return
            fed, budget_hit = self._feed_bytes_under_budget(chunk, deadline)
            total += fed
            if fed < len(chunk):
                self._pending_input.extend(chunk[fed:])
            if budget_hit:
                self._mark_dirty_and_yield(total)
                return
        if total:
            self._render_dirty = True

    def _drain_pending_under_budget(self, deadline: float) -> int:
        """Feed any leftover input; return bytes fed, or ``-1`` if yielded."""
        if not self._pending_input:
            return 0
        fed, budget_hit = self._feed_bytes_under_budget(self._pending_input, deadline)
        del self._pending_input[:fed]
        if budget_hit:
            self._mark_dirty_and_yield(fed)
            return -1
        return fed

    def _read_master_chunk(self) -> bytes | None:
        """Read one chunk from the master, or ``None`` on EAGAIN."""
        if self._master_fd is None:
            return None
        try:
            return os.read(self._master_fd, _READ_CHUNK_BYTES)
        except OSError as error:
            if error.errno in {errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR}:
                return None
            self._on_master_closed()
            return b""

    def _mark_dirty_and_yield(self, total: int) -> None:
        """Mark the widget dirty when bytes were fed, then yield the reader."""
        if total:
            self._render_dirty = True
        self._yield_reader()

    def _feed_bytes_under_budget(
        self, data: bytes | bytearray, deadline: float
    ) -> tuple[int, bool]:
        """Feed ``data`` to pyte in small slices until the time budget is hit.

        Args:
            data: Bytes awaiting ``ByteStream.feed``.
            deadline: ``time.perf_counter()`` deadline for this callback.

        Returns:
            A pair ``(bytes_fed, budget_hit)``.
        """
        if self._stream is None or not data:
            return 0, False
        offset = 0
        length = len(data)
        while offset < length:
            if time.perf_counter() >= deadline:
                return offset, True
            end = min(offset + _FEED_SLICE_BYTES, length)
            self._stream.feed(bytes(data[offset:end]))
            offset = end
            # A single slice can still burn most of the budget; yield after it.
            if time.perf_counter() >= deadline:
                return offset, True
        return offset, False

    def _yield_reader(self) -> None:
        """Pause the master reader so other loop callbacks can run."""
        if self._reader_resume_task is not None and not self._reader_resume_task.done():
            return
        self._remove_reader()

        async def _resume() -> None:
            await asyncio.sleep(0)
            if self._master_fd is None or self._shutting_down:
                return
            self._install_reader()
            # Drain any bytes already pending without waiting for another edge.
            self._on_master_readable()

        self._reader_resume_task = asyncio.create_task(_resume())

    def _close_master_fd(self) -> None:
        """Close the PTY master if it is still open."""
        master_fd = self._master_fd
        self._master_fd = None
        if master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(master_fd)

    def _on_master_closed(self) -> None:
        """Stop reading on EOF/EIO; poll for a natural exit (do not kill yet).

        The master fd stays open until the child is reaped or force-shutdown
        runs. Closing it early would deliver ``SIGHUP`` to a child that only
        redirected stdio away from the tty.
        """
        if self._shutting_down:
            return
        self._remove_reader()
        if self._reap_task is not None and not self._reap_task.done():
            return
        self._reap_task = asyncio.create_task(self._reap_after_master_closed())

    async def _reap_after_master_closed(self) -> None:
        """Poll until the child exits; escalate only after orphan timeout.

        EOF on the master only means the tty side is gone. The child may keep
        running with redirected stdio, so this path never signals until
        ``orphan_timeout_seconds`` elapses. Unmount/restart/atexit use
        :meth:`_shutdown_child` instead.
        """
        pid = self._pid
        if pid is None:
            return
        exit_code = await self._poll_exit(pid, self._orphan_timeout_seconds)
        if exit_code is None and not self._shutting_down:
            self._signal_group(pid, signal.SIGHUP)
            exit_code = await self._poll_exit(pid, _KILL_GRACE_SECONDS)
        if exit_code is None and not self._shutting_down:
            self._signal_group(pid, signal.SIGKILL)
            exit_code = await self._poll_exit(pid, _KILL_GRACE_SECONDS)
        if exit_code is None:
            return
        if self._pid != pid:
            return
        self._close_master_fd()
        self._pid = None
        self.finished_code = exit_code
        self.post_message(self.Finished(exit_code))
        self._render_dirty = True

    async def _poll_exit(self, pid: int, timeout_seconds: float) -> int | None:
        """Poll ``waitpid(WNOHANG)`` until the child exits or timeout elapses."""
        if timeout_seconds == 0:
            return self._try_reap(pid)
        deadline = time.monotonic() + timeout_seconds
        while True:
            reaped = self._try_reap(pid)
            if reaped is not None:
                return reaped
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    def _cursor_should_show(self) -> bool:
        """Whether the cursor cell should be highlighted in the current frame."""
        if self._emulator is None or self.finished_code is not None:
            return False
        cursor_visible = pyte_modes.DECTCEM in self._emulator.mode
        return self.has_focus and cursor_visible

    def _rows_needing_redraw(self, *, rebuild_text: bool) -> set[int]:
        """Collect pyte dirty rows plus cursor rows that must be repainted."""
        emulator = self._emulator
        if emulator is None:
            return set()
        dirty = set(emulator.dirty)
        cursor_row = emulator.cursor.y
        if self._cached_cursor_row is not None and self._cached_cursor_row != cursor_row:
            dirty.add(self._cached_cursor_row)
        if self._cursor_should_show():
            dirty.add(cursor_row)
        # Strip-only flushes clear ``_line_cache``; a later ``render()`` must
        # rebuild every row rather than only the cursor line.
        if (rebuild_text and len(self._line_cache) != emulator.lines) or not dirty:
            return set(range(emulator.lines))
        return dirty

    def _ensure_strip_cache_size(self, lines: int, columns: int) -> None:
        """Grow or shrink the strip cache to match the emulator geometry."""
        while len(self._strip_cache) < lines:
            self._strip_cache.append(Strip.blank(columns, self.rich_style))
        if len(self._strip_cache) > lines:
            del self._strip_cache[lines:]

    def _sync_render_caches(self, *, rebuild_text: bool = False) -> set[int]:
        """Rebuild dirty strip rows (and text on demand); return refreshed rows.

        Args:
            rebuild_text: Also refresh ``_line_cache`` for ``render()`` callers.
        """
        emulator = self._emulator
        if emulator is None:
            return set()
        show_cursor = self._cursor_should_show()
        dirty = self._rows_needing_redraw(rebuild_text=rebuild_text)
        self._ensure_strip_cache_size(emulator.lines, emulator.columns)
        if rebuild_text:
            self._line_cache = [
                self._line_cache[row_index]
                if row_index < len(self._line_cache)
                else Text(" " * emulator.columns)
                for row_index in range(emulator.lines)
            ]
        rebuilt: set[int] = set()
        for row_index in dirty:
            if 0 <= row_index < emulator.lines:
                self._strip_cache[row_index] = render_line_strip(
                    emulator, row_index, show_cursor=show_cursor
                )
                if rebuild_text:
                    self._line_cache[row_index] = render_line_text(
                        emulator, row_index, show_cursor=show_cursor
                    )
                rebuilt.add(row_index)
        overlay_row = self._overlay_exit_status(rebuild_text=rebuild_text)
        if overlay_row is not None:
            rebuilt.add(overlay_row)
        if not rebuild_text and rebuilt:
            self._line_cache.clear()
        emulator.dirty.clear()
        self._cached_cursor_row = emulator.cursor.y if show_cursor else None
        return rebuilt

    def _flush_if_dirty(self) -> None:
        """Refresh dirty rows at most ~30 fps when the emulator changed."""
        if not self._render_dirty:
            return
        self._render_dirty = False
        rebuilt = self._sync_render_caches()
        if not rebuilt:
            return
        width = max(self.content_size.width, 1)
        # Region refreshes keep the compositor on the partial-update path; a
        # blank refresh() marks the full screen dirty and forces ~300 ms paints.
        self.refresh(*(Region(0, row, width, 1) for row in sorted(rebuilt)))

    def _content_dimensions(self) -> tuple[int, int]:
        """Return columns and rows from the border-exclusive content size."""
        size = self.content_size
        return clamp_terminal_dimensions(size.width, size.height)

    def _apply_winsize(self) -> None:
        """Resize pyte and push ``TIOCSWINSZ`` / ``SIGWINCH`` to the child."""
        if self._emulator is None:
            return
        columns, lines = self._content_dimensions()
        if columns != self._emulator.columns or lines != self._emulator.lines:
            # Emulator.resize clears scrollback; drop local render caches too.
            self._emulator.resize(lines=lines, columns=columns)
            self._line_cache.clear()
            self._strip_cache.clear()
            self._cached_cursor_row = None
            self._render_dirty = True
        if self._master_fd is None or self._pid is None:
            return
        packed = struct.pack("HHHH", lines, columns, 0, 0)
        try:
            fcntl.ioctl(self._master_fd, termios_tiocswinsz(), packed)
            os.kill(self._pid, signal.SIGWINCH)
        except OSError:
            logger.debug("Failed to deliver PTY window size to child pid=%s", self._pid)

    def _write_master(self, data: bytes) -> None:
        """Queue bytes for the master fd; retry on ``EAGAIN`` via ``add_writer``."""
        if self._master_fd is None or not data:
            return
        remaining = _MAX_WRITE_BUFFER_BYTES - len(self._write_buffer)
        if remaining <= 0:
            return
        self._write_buffer.extend(data[:remaining])
        self._flush_write_buffer()

    def _flush_write_buffer(self) -> None:
        """Write as much of the outbound buffer as the kernel will accept."""
        if self._master_fd is None:
            self._write_buffer.clear()
            self._remove_writer()
            return
        while self._write_buffer:
            try:
                written = os.write(self._master_fd, self._write_buffer)
            except OSError as error:
                if error.errno in {errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR}:
                    self._install_writer()
                    return
                logger.debug("PTY master write failed for pid=%s", self._pid)
                self._write_buffer.clear()
                self._remove_writer()
                return
            if written == 0:
                self._install_writer()
                return
            del self._write_buffer[:written]
        self._remove_writer()

    def _cancel_reap_task(self) -> None:
        """Cancel an in-flight async reap when forcing shutdown or restart."""
        task = self._reap_task
        self._reap_task = None
        if task is not None and not task.done():
            task.cancel()
        resume = self._reader_resume_task
        self._reader_resume_task = None
        if resume is not None and not resume.done():
            resume.cancel()

    def _shutdown_child(self, *, grace_seconds: float) -> int:
        """Force-close the master, signal the process group, and reap.

        Used for unmount, restart, and atexit. Master EOF alone only polls for
        a natural exit (see :meth:`_reap_after_master_closed`).

        Args:
            grace_seconds: Delay between ``SIGHUP`` and ``SIGKILL``.

        Returns:
            Exit code when known, otherwise ``-1``.
        """
        self._cancel_reap_task()
        self._shutting_down = True
        self._remove_reader()
        self._remove_writer()
        self._write_buffer.clear()
        self._pending_input.clear()
        self._close_master_fd()

        pid = self._pid
        exit_code = -1
        if pid is not None:
            self._signal_group(pid, signal.SIGHUP)
            if grace_seconds > 0:
                deadline = time.monotonic() + grace_seconds
                while time.monotonic() < deadline:
                    reaped = self._try_reap(pid)
                    if reaped is not None:
                        exit_code = reaped
                        break
                    time.sleep(0.01)
            if exit_code == -1:
                self._signal_group(pid, signal.SIGKILL)
                reaped = self._try_reap(pid, block=True)
                if reaped is not None:
                    exit_code = reaped
        self._pid = None
        # Keep the last screen for display; restart replaces the emulator.
        self._stream = None
        return exit_code

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        """Send ``sig`` to the child's process group, ignoring missing pids."""
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
        except PermissionError:
            logger.debug("Permission denied signaling PTY child pid=%s", pid)

    @staticmethod
    def _try_reap(pid: int, *, block: bool = False) -> int | None:
        """Reap ``pid`` and return its exit code when available."""
        flags = 0 if block else os.WNOHANG
        try:
            finished_pid, status = os.waitpid(pid, flags)
        except ChildProcessError:
            return -1
        if finished_pid == 0:
            return None
        return os.waitstatus_to_exitcode(status)


def termios_tiocswinsz() -> int:
    """Return ``TIOCSWINSZ`` from termios (lazy import for non-POSIX typing)."""
    import termios

    return termios.TIOCSWINSZ
