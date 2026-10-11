"""Render a pyte screen buffer as Rich text / Textual strips."""

from __future__ import annotations

from pyte.screens import Char, Screen
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.strip import Strip

_ANSI_COLOR_ALIASES: dict[str, str] = {
    "brown": "yellow",
    "brightbrown": "bright_yellow",
    "brightblack": "bright_black",
    "brightred": "bright_red",
    "brightgreen": "bright_green",
    "brightblue": "bright_blue",
    "brightmagenta": "bright_magenta",
    "brightcyan": "bright_cyan",
    "brightwhite": "bright_white",
}

_StyleKey = tuple[str, str, bool, bool, bool, bool, bool, bool]
_STYLE_CACHE: dict[_StyleKey, Style] = {}


def _resolve_color(name: str) -> str | None:
    """Map a pyte color token to a Rich color string."""
    if name == "default":
        return None
    if len(name) == 6 and all(char in "0123456789abcdefABCDEF" for char in name):
        return f"#{name}"
    return _ANSI_COLOR_ALIASES.get(name, name)


def _style_key(char: Char, *, reverse: bool) -> _StyleKey:
    """Build a cache key for a cell's visible attributes."""
    return (
        char.fg,
        char.bg,
        char.bold,
        char.italics,
        char.underscore,
        char.strikethrough,
        char.blink,
        reverse,
    )


def cached_char_style(char: Char, *, reverse: bool | None = None) -> Style:
    """Return a cached Rich style for a pyte cell.

    Args:
        char: pyte character cell.
        reverse: Override reverse-video; defaults to the cell's reverse flag.
    """
    use_reverse = char.reverse if reverse is None else reverse
    key = _style_key(char, reverse=use_reverse)
    style = _STYLE_CACHE.get(key)
    if style is not None:
        return style
    style = Style(
        color=_resolve_color(char.fg),
        bgcolor=_resolve_color(char.bg),
        bold=char.bold,
        italic=char.italics,
        underline=char.underscore,
        strike=char.strikethrough,
        blink=char.blink,
        reverse=use_reverse,
    )
    _STYLE_CACHE[key] = style
    return style


def _iter_styled_runs(
    emulator: Screen,
    row_index: int,
    *,
    show_cursor: bool,
) -> list[tuple[str, Style]]:
    """Group adjacent cells on one row into ``(text, style)`` runs."""
    row = emulator.buffer[row_index]
    columns = emulator.columns
    cursor_x = emulator.cursor.x
    cursor_on_row = show_cursor and row_index == emulator.cursor.y
    # Fast path: monochrome rows (typical ``yes`` / ``cat`` floods) become one run.
    if not cursor_on_row and columns:
        first = row[0]
        uniform = True
        for column_index in range(1, columns):
            char = row[column_index]
            if (
                char.fg != first.fg
                or char.bg != first.bg
                or char.bold != first.bold
                or char.italics != first.italics
                or char.underscore != first.underscore
                or char.strikethrough != first.strikethrough
                or char.blink != first.blink
                or char.reverse != first.reverse
            ):
                uniform = False
                break
        if uniform:
            text = "".join(row[column_index].data for column_index in range(columns))
            return [(text, cached_char_style(first))]

    runs: list[tuple[str, Style]] = []
    run_chars: list[str] = []
    run_style: Style | None = None
    for column_index in range(columns):
        char = row[column_index]
        is_cursor = cursor_on_row and column_index == cursor_x
        # Toggle reverse so the cursor stays visible on reverse-video cells.
        reverse = (not char.reverse) if is_cursor else char.reverse
        style = cached_char_style(char, reverse=reverse)
        if run_style is None or style is not run_style:
            if run_chars and run_style is not None:
                runs.append(("".join(run_chars), run_style))
            run_chars = [char.data]
            run_style = style
        else:
            run_chars.append(char.data)
    if run_chars and run_style is not None:
        runs.append(("".join(run_chars), run_style))
    return runs


def render_line_text(
    emulator: Screen,
    row_index: int,
    *,
    show_cursor: bool,
) -> Text:
    """Render one emulator row with run-length style grouping.

    Args:
        emulator: Active pyte screen.
        row_index: Zero-based row to render.
        show_cursor: Whether to mark the cursor cell on this row.

    Returns:
        A single-line Rich ``Text`` (no trailing newline).
    """
    output = Text()
    for text, style in _iter_styled_runs(emulator, row_index, show_cursor=show_cursor):
        output.append(text, style=style)
    return output


def render_line_strip(
    emulator: Screen,
    row_index: int,
    *,
    show_cursor: bool,
) -> Strip:
    """Render one emulator row as a Textual ``Strip`` of coalesced segments.

    Args:
        emulator: Active pyte screen.
        row_index: Zero-based row to render.
        show_cursor: Whether to mark the cursor cell on this row.

    Returns:
        A strip whose cell length matches ``emulator.columns``.
    """
    segments = [
        Segment(text, style)
        for text, style in _iter_styled_runs(emulator, row_index, show_cursor=show_cursor)
    ]
    return Strip(segments, emulator.columns)


def render_emulator(
    emulator: Screen,
    *,
    show_cursor: bool,
    line_cache: list[Text] | None = None,
    force_rows: set[int] | None = None,
) -> Text:
    """Render the visible pyte buffer, optionally reusing cached lines.

    Args:
        emulator: Active pyte screen (not named ``screen``; Textual owns that).
        show_cursor: Whether to reverse the cell under the cursor.
        line_cache: Optional per-row cache updated from ``emulator.dirty``.
        force_rows: Extra rows to rebuild (for example previous cursor row).

    Returns:
        A Rich ``Text`` suitable for a Textual widget ``render`` method.
    """
    lines = emulator.lines
    columns = emulator.columns
    if line_cache is not None:
        while len(line_cache) < lines:
            line_cache.append(Text(" " * columns))
        if len(line_cache) > lines:
            del line_cache[lines:]
        dirty = set(emulator.dirty)
        if force_rows:
            dirty.update(force_rows)
        if not dirty:
            dirty = set(range(lines))
        for row_index in dirty:
            if 0 <= row_index < lines:
                line_cache[row_index] = render_line_text(
                    emulator, row_index, show_cursor=show_cursor
                )
        emulator.dirty.clear()
        output = Text()
        for row_index, line in enumerate(line_cache[:lines]):
            if row_index:
                output.append("\n")
            output.append_text(line)
        return output

    output = Text()
    for row_index in range(lines):
        if row_index:
            output.append("\n")
        output.append_text(render_line_text(emulator, row_index, show_cursor=show_cursor))
    emulator.dirty.clear()
    return output
