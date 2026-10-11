"""Map Textual key and paste events to xterm/VT100 byte sequences."""

from __future__ import annotations

from textual.events import Key

# Private mode 1 (DECCKM) is stored shifted in pyte's mode set.
_DECCKM_MODE = 1 << 5
_BRACKETED_PASTE_START = b"\x1b[200~"
_BRACKETED_PASTE_END = b"\x1b[201~"
_BRACKETED_PASTE_MODE = 2004 << 5

_ARROW_FINAL: dict[str, str] = {
    "up": "A",
    "down": "B",
    "right": "C",
    "left": "D",
}
_HOME_END_FINAL: dict[str, str] = {
    "home": "H",
    "end": "F",
}
_TILDE_KEYS: dict[str, int] = {
    "insert": 2,
    "delete": 3,
    "pageup": 5,
    "pagedown": 6,
}
_FKEY_SS3: dict[str, str] = {
    "f1": "P",
    "f2": "Q",
    "f3": "R",
    "f4": "S",
}
_FKEY_TILDE: dict[str, int] = {
    "f5": 15,
    "f6": 17,
    "f7": 18,
    "f8": 19,
    "f9": 20,
    "f10": 21,
    "f11": 23,
    "f12": 24,
}
_SIMPLE_KEYS: dict[str, bytes] = {
    "backspace": b"\x7f",
    "tab": b"\t",
    "shift+tab": b"\x1b[Z",
    "enter": b"\r",
    "escape": b"\x1b",
}


def application_cursor_keys(modes: set[int]) -> bool:
    """Return whether DECCKM (private mode 1) is active on the pyte screen."""
    return _DECCKM_MODE in modes


def bracketed_paste_enabled(modes: set[int]) -> bool:
    """Return whether pyte reports private mode 2004 as active."""
    return _BRACKETED_PASTE_MODE in modes


def _modifier_code(parts: set[str]) -> int | None:
    """Build the xterm modifier parameter (None when unmodified)."""
    value = 1
    if "shift" in parts:
        value += 1
    if "alt" in parts or "meta" in parts:
        value += 2
    if "ctrl" in parts or "control" in parts:
        value += 4
    return None if value == 1 else value


def _encode_csi_final(final: str, *, modifier: int | None) -> bytes:
    if modifier is None:
        return f"\x1b[{final}".encode()
    return f"\x1b[1;{modifier}{final}".encode()


def _encode_csi_tilde(number: int, *, modifier: int | None) -> bytes:
    if modifier is None:
        return f"\x1b[{number}~".encode()
    return f"\x1b[{number};{modifier}~".encode()


def _encode_arrow(base: str, *, modifier: int | None, application: bool) -> bytes:
    final = _ARROW_FINAL[base]
    if modifier is not None:
        return _encode_csi_final(final, modifier=modifier)
    if application:
        return f"\x1bO{final}".encode()
    return f"\x1b[{final}".encode()


def _encode_home_end(base: str, *, modifier: int | None, application: bool) -> bytes:
    final = _HOME_END_FINAL[base]
    if modifier is not None:
        return _encode_csi_final(final, modifier=modifier)
    if application:
        return f"\x1bO{final}".encode()
    return f"\x1b[{final}".encode()


def _encode_fkey(base: str, *, modifier: int | None) -> bytes | None:
    if base in _FKEY_SS3:
        final = _FKEY_SS3[base]
        if modifier is None:
            return f"\x1bO{final}".encode()
        return _encode_csi_final(final, modifier=modifier)
    number = _FKEY_TILDE.get(base)
    if number is None:
        return None
    return _encode_csi_tilde(number, modifier=modifier)


def key_to_bytes(event: Key, modes: set[int] | None = None) -> bytes | None:
    """Translate a Textual key event into bytes for the PTY child.

    Args:
        event: Focused-widget key event from Textual.
        modes: Active pyte screen modes (for DECCKM / application cursors).

    Returns:
        Bytes to write to the PTY master, or ``None`` when the key has no
        terminal encoding (for example pointer-only chords).
    """
    active_modes = modes if modes is not None else set()
    key = event.key
    simple = _SIMPLE_KEYS.get(key)
    if simple is not None:
        return simple

    parts = set(key.split("+"))
    modifier_names = {"shift", "alt", "meta", "ctrl", "control"}
    base = next((part for part in parts if part not in modifier_names), None)
    if base is None:
        return None
    modifier = _modifier_code(parts)

    if base in _ARROW_FINAL:
        return _encode_arrow(
            base,
            modifier=modifier,
            application=application_cursor_keys(active_modes),
        )
    if base in _HOME_END_FINAL:
        return _encode_home_end(
            base,
            modifier=modifier,
            application=application_cursor_keys(active_modes),
        )
    if base in _TILDE_KEYS:
        return _encode_csi_tilde(_TILDE_KEYS[base], modifier=modifier)
    fkey = _encode_fkey(base, modifier=modifier)
    if fkey is not None:
        return fkey

    if "ctrl" in parts and len(base) == 1 and base.isalpha() and modifier == 5:
        # Plain Ctrl+letter (no other modifiers).
        return bytes([ord(base.lower()) - ord("a") + 1])

    if parts - modifier_names == {base} and parts & {"alt", "meta"} and len(base) == 1:
        return b"\x1b" + base.encode("utf-8", errors="replace")

    if event.character and modifier is None and not (parts & {"alt", "meta"}):
        return event.character.encode("utf-8", errors="replace")
    return None


def paste_to_bytes(text: str, *, bracketed: bool) -> bytes:
    """Encode a paste payload, wrapping it when bracketed-paste mode is on.

    Args:
        text: Pasted Unicode text from Textual.
        bracketed: Whether the child enabled DEC mode 2004.

    Returns:
        Bytes to write to the PTY master.
    """
    payload = text.encode("utf-8", errors="replace")
    if not bracketed:
        return payload
    return _BRACKETED_PASTE_START + payload + _BRACKETED_PASTE_END
