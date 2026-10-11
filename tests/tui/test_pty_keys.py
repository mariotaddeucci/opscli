"""Unit tests for Textual-to-PTY key and paste encoding."""

import pytest
from textual.events import Key

from curupira.tui.pty_keys import (
    application_cursor_keys,
    bracketed_paste_enabled,
    key_to_bytes,
    paste_to_bytes,
)

_DECCKM = 1 << 5


def test_arrow_and_editing_keys_use_xterm_sequences() -> None:
    assert key_to_bytes(Key("up", character=None)) == b"\x1b[A"
    assert key_to_bytes(Key("home", character=None)) == b"\x1b[H"
    assert key_to_bytes(Key("end", character=None)) == b"\x1b[F"
    assert key_to_bytes(Key("pageup", character=None)) == b"\x1b[5~"
    assert key_to_bytes(Key("pagedown", character=None)) == b"\x1b[6~"
    assert key_to_bytes(Key("backspace", character=None)) == b"\x7f"
    assert key_to_bytes(Key("tab", character="\t")) == b"\t"
    assert key_to_bytes(Key("shift+tab", character=None)) == b"\x1b[Z"
    assert key_to_bytes(Key("enter", character="\r")) == b"\r"
    assert key_to_bytes(Key("escape", character=None)) == b"\x1b"


def test_decckm_switches_arrow_and_home_end_to_ss3() -> None:
    assert application_cursor_keys({_DECCKM})
    assert key_to_bytes(Key("up", character=None), modes={_DECCKM}) == b"\x1bOA"
    assert key_to_bytes(Key("down", character=None), modes={_DECCKM}) == b"\x1bOB"
    assert key_to_bytes(Key("right", character=None), modes={_DECCKM}) == b"\x1bOC"
    assert key_to_bytes(Key("left", character=None), modes={_DECCKM}) == b"\x1bOD"
    assert key_to_bytes(Key("home", character=None), modes={_DECCKM}) == b"\x1bOH"
    assert key_to_bytes(Key("end", character=None), modes={_DECCKM}) == b"\x1bOF"
    assert key_to_bytes(Key("up", character=None), modes=set()) == b"\x1b[A"
    assert key_to_bytes(Key("home", character=None), modes=set()) == b"\x1b[H"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("ctrl+up", b"\x1b[1;5A"),
        ("shift+down", b"\x1b[1;2B"),
        ("alt+right", b"\x1b[1;3C"),
        ("ctrl+shift+left", b"\x1b[1;6D"),
        ("ctrl+home", b"\x1b[1;5H"),
        ("ctrl+end", b"\x1b[1;5F"),
        ("shift+f5", b"\x1b[15;2~"),
        ("ctrl+f1", b"\x1b[1;5P"),
        ("alt+f12", b"\x1b[24;3~"),
        ("shift+pageup", b"\x1b[5;2~"),
    ],
)
def test_modified_keys_use_xterm_modifier_encoding(key: str, expected: bytes) -> None:
    assert key_to_bytes(Key(key, character=None)) == expected


def test_function_keys_and_plain_modifiers() -> None:
    assert key_to_bytes(Key("f1", character=None)) == b"\x1bOP"
    assert key_to_bytes(Key("f12", character=None)) == b"\x1b[24~"
    assert key_to_bytes(Key("ctrl+c", character="\x03")) == b"\x03"
    assert key_to_bytes(Key("alt+a", character=None)) == b"\x1ba"
    assert key_to_bytes(Key("a", character="a")) == b"a"


def test_bracketed_paste_wrapping_follows_mode_flag() -> None:
    assert paste_to_bytes("hi", bracketed=False) == b"hi"
    assert paste_to_bytes("hi", bracketed=True) == b"\x1b[200~hi\x1b[201~"
    assert bracketed_paste_enabled({2004 << 5})
    assert not bracketed_paste_enabled(set())
