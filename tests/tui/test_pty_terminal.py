"""Behavioral tests for the reusable PtyTerminal Textual widget."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import math
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult
from textual.events import Key, Paste
from textual.widgets import Static
from typing_extensions import override

from curupira.tui.pty_keys import key_to_bytes
from curupira.tui.pty_terminal import (
    PtyTerminal,
    clamp_terminal_dimensions,
    default_pty_environment,
)


class _FocusTarget(Static, can_focus=True):
    """Focusable sibling used to observe escape-key focus handoff."""


class _PtyHarness(App[None]):
    """Minimal Textual app that mounts one PtyTerminal under test."""

    CSS = """
    Screen { layout: vertical; }
    #other { height: 1; }
    PtyTerminal { width: 1fr; height: 1fr; }
    """

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        escape_key: str = "ctrl+g",
    ) -> None:
        super().__init__()
        self._argv = argv
        self._cwd = cwd
        self._escape_key = escape_key
        self.finished_codes: list[int] = []
        self.focus_released = False

    @override
    def compose(self) -> ComposeResult:
        yield _FocusTarget("other", id="other")
        yield PtyTerminal(
            self._argv,
            env=default_pty_environment(),
            cwd=self._cwd,
            escape_key=self._escape_key,
            id="pty",
        )

    def on_pty_terminal_finished(self, message: PtyTerminal.Finished) -> None:
        self.finished_codes.append(message.exit_code)

    def on_pty_terminal_focus_released(self, message: PtyTerminal.FocusReleased) -> None:
        del message
        self.focus_released = True
        self.query_one("#other", _FocusTarget).focus()


def _visible_text(terminal: PtyTerminal) -> str:
    return str(terminal.render())


async def _wait_until(predicate: Callable[[], bool], pilot: Any, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met before timeout")


def test_default_pty_environment_is_allowlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("USER", "tester")
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("SECRET", "nope")
    environment = default_pty_environment()
    assert environment["PATH"] == "/usr/bin"
    assert environment["HOME"] == "/home/test"
    assert environment["TERM"] == "xterm-256color"
    assert environment["COLORTERM"] == "truecolor"
    assert "SECRET" not in environment


def test_windows_guard_builds_unsupported_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "name", "nt")
    terminal = PtyTerminal(["echo", "hi"])
    assert not terminal.supported
    assert "Windows" in str(terminal.render())


def test_clamp_terminal_dimensions_uses_one_by_one_for_zero() -> None:
    assert clamp_terminal_dimensions(0, 0) == (1, 1)
    assert clamp_terminal_dimensions(0, 5) == (1, 5)
    assert clamp_terminal_dimensions(3, 0) == (3, 1)
    assert clamp_terminal_dimensions(80, 24) == (80, 24)


def test_emulator_scrollback_retains_scrolled_off_rows() -> None:
    """Cheap Screen scrollback must keep discarded top rows without HistoryScreen."""
    import pyte

    from curupira.tui import pty_terminal as pty_terminal_module

    writes: list[bytes] = []
    emulator = pty_terminal_module._EmulatorScreen(
        8,
        2,
        history=8,
        on_write=writes.append,
    )
    stream = pyte.ByteStream(emulator)
    stream.feed(b"AAAA\nBBBB\nCCCC\n")
    assert len(emulator.scrollback) >= 1
    retained = [
        "".join(row[column].data for column in range(emulator.columns)).rstrip()
        for row in emulator.scrollback
    ]
    assert "AAAA" in retained
    assert writes == []


def test_emulator_scrollback_clears_on_reset_and_resize() -> None:
    import pyte

    from curupira.tui import pty_terminal as pty_terminal_module

    emulator = pty_terminal_module._EmulatorScreen(
        8,
        2,
        history=8,
        on_write=lambda _payload: None,
    )
    stream = pyte.ByteStream(emulator)
    stream.feed(b"AAAA\nBBBB\nCCCC\n")
    assert emulator.scrollback
    emulator.reset()
    assert len(emulator.scrollback) == 0

    stream.feed(b"DDDD\nEEEE\nFFFF\n")
    assert emulator.scrollback
    emulator.resize(lines=3, columns=8)
    assert len(emulator.scrollback) == 0


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_typed_input_echoes_through_cat() -> None:
    app = _PtyHarness(["cat"])
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        terminal.focus()
        await _wait_until(lambda: terminal.pid is not None, pilot)
        await pilot.press("h", "i", "enter")
        await _wait_until(lambda: "hi" in _visible_text(terminal), pilot)
        assert "hi" in _visible_text(terminal)
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_resize_updates_tput_cols_and_lines() -> None:
    app = _PtyHarness(
        [
            "bash",
            "-c",
            'while true; do printf \'%s %s\\n\' "$(tput cols)" "$(tput lines)"; sleep 0.05; done',
        ]
    )
    async with app.run_test(size=(60, 20)) as pilot:
        terminal = app.query_one(PtyTerminal)
        await _wait_until(lambda: terminal.content_size.width > 1, pilot)
        await _wait_until(lambda: terminal._emulator is not None, pilot)

        def _synced() -> bool:
            assert terminal._emulator is not None
            terminal._apply_winsize()
            expected = f"{terminal.content_size.width} {terminal.content_size.height}"
            return (
                terminal._emulator.columns == terminal.content_size.width
                and terminal._emulator.lines == terminal.content_size.height
                and expected in _visible_text(terminal)
            )

        await _wait_until(_synced, pilot)

        await pilot.resize_terminal(100, 40)
        await _wait_until(lambda: terminal.content_size.width >= 90, pilot)
        await _wait_until(_synced, pilot)
        expected = f"{terminal.content_size.width} {terminal.content_size.height}"
        assert expected in _visible_text(terminal)
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_escape_key_releases_focus_and_ctrl_c_reaches_child(tmp_path: Path) -> None:
    marker = tmp_path / "sigint.txt"
    child = f"""
import signal, sys, pathlib, time
path = pathlib.Path({str(marker)!r})
path.write_text("ready")
def handle(signum, frame):
    path.write_text("interrupted")
    sys.exit(0)
signal.signal(signal.SIGINT, handle)
while True:
    time.sleep(0.05)
"""
    app = _PtyHarness([sys.executable, "-c", child])
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        other = app.query_one("#other", _FocusTarget)
        terminal.focus()
        await _wait_until(lambda: terminal.has_focus, pilot)

        await pilot.press("ctrl+g")
        await _wait_until(lambda: app.focus_released and other.has_focus, pilot)
        assert not terminal.has_focus

        terminal.focus()
        await _wait_until(lambda: marker.exists() and marker.read_text() == "ready", pilot)
        await pilot.press("ctrl+c")
        await _wait_until(lambda: marker.read_text() == "interrupted", pilot)
        assert marker.read_text() == "interrupted"
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_bracketed_paste_is_wrapped_when_child_enables_mode(tmp_path: Path) -> None:
    marker = tmp_path / "paste.bin"
    child = f"""
import os, sys, tty
sys.stdout.write("\\x1b[?2004hREADY\\n")
sys.stdout.flush()
tty.setraw(0)
data = os.read(0, 200)
open({str(marker)!r}, "wb").write(data)
"""
    app = _PtyHarness([sys.executable, "-c", child])
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        terminal.focus()
        await _wait_until(lambda: "READY" in _visible_text(terminal), pilot)
        await _wait_until(
            lambda: terminal._emulator is not None and (2004 << 5) in terminal._emulator.mode,
            pilot,
        )
        terminal.on_paste(Paste("hello"))
        await _wait_until(lambda: marker.exists(), pilot)
        assert marker.read_bytes() == b"\x1b[200~hello\x1b[201~"
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_child_exit_keeps_screen_and_shows_footer() -> None:
    app = _PtyHarness(["bash", "-c", "printf 'VISIBLE\\n'; exit 42"])
    async with app.run_test(size=(80, 24)) as pilot:
        await _wait_until(lambda: app.finished_codes == [42], pilot)
        terminal = app.query_one(PtyTerminal)
        assert terminal.finished_code == 42
        text = _visible_text(terminal)
        assert "VISIBLE" in text
        assert "Process exited (42)" in text
        assert terminal._emulator is not None
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_exit_status_appears_within_content_height() -> None:
    """Exit banner must paint inside content rows, not past the last line."""
    # Fill the viewport so a trailing footer row would be clipped.
    app = _PtyHarness(["bash", "-c", "python3 -c \"print('LINE'); print('x'*2000)\"; exit 7"])
    async with app.run_test(size=(40, 8)) as pilot:
        await _wait_until(lambda: app.finished_codes == [7], pilot, attempts=200)
        terminal = app.query_one(PtyTerminal)
        await pilot.pause(0.05)
        content_height = terminal.content_size.height
        assert content_height >= 1
        on_screen = [terminal.render_line(row).text for row in range(content_height)]
        assert any("Process exited (7)" in row for row in on_screen)
        assert "Process exited (7)" in _visible_text(terminal)
        # A row past the content height must not be required for the banner.
        assert "Process exited (7)" not in terminal.render_line(content_height).text
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, 3])
async def test_normal_exit_codes_are_preserved(code: int) -> None:
    app = _PtyHarness(["bash", "-c", f"exit {code}"])
    async with app.run_test(size=(80, 24)) as pilot:
        await _wait_until(lambda: app.finished_codes == [code], pilot)
        assert app.finished_codes == [code]
        assert app.query_one(PtyTerminal).finished_code == code
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_bad_cwd_exits_with_127() -> None:
    app = _PtyHarness(["true"], cwd=Path("/nonexistent-curupira-pty-cwd"))
    async with app.run_test(size=(80, 24)) as pilot:
        await _wait_until(lambda: app.finished_codes == [127], pilot)
        assert app.query_one(PtyTerminal).finished_code == 127
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_sigpipe_restored_for_shell_pipeline() -> None:
    app = _PtyHarness(["bash", "-c", "yes | head -n 3; printf 'PIPELINE_OK\\n'"])
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        await _wait_until(lambda: "PIPELINE_OK" in _visible_text(terminal), pilot)
        text = _visible_text(terminal)
        assert "Broken pipe" not in text
        assert "y" in text
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_decckm_arrows_are_forwarded_from_widget(tmp_path: Path) -> None:
    marker = tmp_path / "keys.bin"
    child = f"""
import os, sys, tty
sys.stdout.write("\\x1b[?1hREADY\\n")
sys.stdout.flush()
tty.setraw(0)
data = os.read(0, 20)
open({str(marker)!r}, "wb").write(data)
"""
    app = _PtyHarness([sys.executable, "-c", child])
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        terminal.focus()
        await _wait_until(lambda: "READY" in _visible_text(terminal), pilot)
        await _wait_until(
            lambda: terminal._emulator is not None and (1 << 5) in terminal._emulator.mode,
            pilot,
        )
        modes = terminal._emulator.mode if terminal._emulator is not None else set()
        payload = key_to_bytes(Key("up", character=None), modes=modes)
        assert payload == b"\x1bOA"
        terminal.write(payload)
        await _wait_until(lambda: marker.exists(), pilot)
        assert marker.read_bytes() == b"\x1bOA"
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_child_closing_tty_keeps_running_and_reports_real_exit(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "survived"
    app = _PtyHarness(
        [
            "sh",
            "-c",
            f"exec >/dev/null 2>&1 </dev/null; sleep 1; touch {marker}; exit 5",
        ]
    )
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        await _wait_until(lambda: terminal.pid is not None, pilot)
        # Master EOF must not kill the child before it finishes its work.
        await _wait_until(lambda: marker.exists(), pilot, attempts=60)
        await _wait_until(lambda: app.finished_codes == [5], pilot, attempts=60)
        assert terminal.finished_code == 5
        assert marker.exists()
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_unmount_kills_child_that_closed_its_tty(tmp_path: Path) -> None:
    marker = tmp_path / "should_not_exist"
    app = _PtyHarness(
        [
            "sh",
            "-c",
            f"exec >/dev/null 2>&1 </dev/null; sleep 30; touch {marker}",
        ]
    )
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        await _wait_until(lambda: terminal.pid is not None, pilot)
        pid = terminal.pid
        assert pid is not None
        # Reader stops on EOF, but the master stays open until force-shutdown.
        await _wait_until(
            lambda: terminal._reap_task is not None and not terminal._reader_installed,
            pilot,
            attempts=40,
        )
        assert terminal.pid == pid
        app.exit()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_unmount_kills_and_reaps_child() -> None:
    app = _PtyHarness(["bash", "-c", "cat"])
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        await _wait_until(lambda: terminal.pid is not None, pilot)
        pid = terminal.pid
        assert pid is not None
        os.kill(pid, 0)
        app.exit()
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    def _reap() -> None:
        os.waitpid(pid, os.WNOHANG)

    with pytest.raises(ChildProcessError):
        await asyncio.to_thread(_reap)


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_write_and_restart_replace_running_child() -> None:
    app = _PtyHarness(["cat"])
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        terminal.focus()
        await _wait_until(lambda: terminal.pid is not None, pilot)
        await pilot.press("p", "i", "n", "g", "enter")
        await _wait_until(lambda: "ping" in _visible_text(terminal), pilot)
        old_pid = terminal.pid
        assert old_pid is not None
        terminal.write("via-write\n")
        await _wait_until(lambda: "via-write" in _visible_text(terminal), pilot)
        terminal.restart()
        await _wait_until(lambda: terminal.pid is not None and terminal.pid != old_pid, pilot)
        assert terminal.pid != old_pid
        assert terminal.finished_code is None
        with pytest.raises(ProcessLookupError):
            os.kill(old_pid, 0)
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_restart_after_exit_keeps_working() -> None:
    app = _PtyHarness(["bash", "-c", "printf 'FIRST\\n'; exit 0"])
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        await _wait_until(lambda: app.finished_codes == [0], pilot)
        assert "FIRST" in _visible_text(terminal)
        terminal.restart()
        await _wait_until(
            lambda: terminal.pid is not None and terminal.finished_code is None,
            pilot,
        )
        # Same argv runs again and finishes.
        await _wait_until(lambda: terminal.finished_code == 0, pilot)
        assert "FIRST" in _visible_text(terminal)
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_zero_content_size_clamps_emulator_to_one_by_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from textual.geometry import Size

    class ZeroHarness(App[None]):
        @override
        def compose(self) -> ComposeResult:
            yield PtyTerminal(["bash", "-c", "sleep 1"], env=default_pty_environment(), id="pty")

    app = ZeroHarness()
    async with app.run_test(size=(40, 20)) as pilot:
        terminal = app.query_one(PtyTerminal)

        def _zero_content_size(_self: PtyTerminal) -> Size:
            return Size(0, 0)

        monkeypatch.setattr(type(terminal), "content_size", property(_zero_content_size))
        assert terminal._content_dimensions() == (1, 1)
        # Without the clamp, a zero size used to become the 80x24 default.
        assert clamp_terminal_dimensions(0, 0) != (80, 24)
        assert clamp_terminal_dimensions(0, 0) == (1, 1)
        terminal._apply_winsize()
        await _wait_until(lambda: terminal._emulator is not None, pilot)
        assert terminal._emulator is not None
        assert terminal._emulator.columns == 1
        assert terminal._emulator.lines == 1
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_flood_keeps_event_loop_ticking() -> None:
    app = _PtyHarness(["bash", "-c", "yes | head -c 500000; printf 'FLOOD_DONE\\n'"])
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        await _wait_until(lambda: terminal.pid is not None, pilot)
        gaps: list[float] = []
        previous = time.perf_counter()

        async def _ticker() -> None:
            nonlocal previous
            while "FLOOD_DONE" not in _visible_text(terminal) and terminal.pid is not None:
                await asyncio.sleep(0)
                now = time.perf_counter()
                gaps.append(now - previous)
                previous = now

        ticker = asyncio.create_task(_ticker())
        await _wait_until(lambda: "FLOOD_DONE" in _visible_text(terminal), pilot, attempts=400)
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker
        assert gaps
        ordered = sorted(gaps)
        p99_index = max(0, math.ceil(len(ordered) * 0.99) - 1)
        assert ordered[p99_index] < 0.050
        assert ordered[-1] < 0.150
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_write_retries_after_eagain(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _PtyHarness(["cat"])
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        await _wait_until(
            lambda: terminal.pid is not None and terminal._master_fd is not None,
            pilot,
        )
        master_fd = terminal._master_fd
        assert master_fd is not None
        real_write = os.write
        calls = {"n": 0}

        def flaky_write(fd: int, data: bytes | bytearray | memoryview) -> int:
            if fd == master_fd and calls["n"] == 0:
                calls["n"] += 1
                raise OSError(errno.EAGAIN, "Resource temporarily unavailable")
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", flaky_write)
        terminal.write("retried\n")
        await _wait_until(lambda: "retried" in _visible_text(terminal), pilot)
        assert calls["n"] == 1
        assert terminal._write_buffer == bytearray()
        app.exit()


@pytest.mark.skipif(os.name != "posix", reason="PtyTerminal v1 requires POSIX")
@pytest.mark.asyncio
async def test_sgr_truecolor_and_alt_screen_sequences_render(tmp_path: Path) -> None:
    script = tmp_path / "sgr.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('\\x1b[38;2;0;128;255mBLUE\\x1b[0m\\n')\n"
        "sys.stdout.write('\\x1b[?1049hALT\\x1b[?1049l')\n"
        "sys.stdout.write('DONE\\n')\n"
        "sys.stdout.flush()\n"
        "import time; time.sleep(0.5)\n",
        encoding="utf-8",
    )
    app = _PtyHarness([sys.executable, str(script)])
    async with app.run_test(size=(80, 24)) as pilot:
        terminal = app.query_one(PtyTerminal)
        await _wait_until(lambda: "DONE" in _visible_text(terminal), pilot)
        text = _visible_text(terminal)
        assert "DONE" in text
        assert "BLUE" in text
        rendered = terminal.render()
        assert isinstance(rendered, Text)
        console = Console(force_terminal=True, color_system="truecolor")
        blue_style = None
        for index, char in enumerate(rendered.plain):
            if char == "B":
                blue_style = rendered.get_style_at_offset(console, index)
                break
        assert blue_style is not None
        assert blue_style.color is not None
        assert blue_style.color.triplet is not None
        assert blue_style.color.triplet.blue == 255
        app.exit()
