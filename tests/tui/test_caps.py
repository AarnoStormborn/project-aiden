"""Terminal capability detection, overrides, and OSC 8 hyperlinks.

research/06 §What great agent UIs do #15: capability detection with user overrides. Every
capability here has a safe default because the failure mode is garbage in the user's output.
"""

from __future__ import annotations

from aiden.tui.caps import OSC8_END, TerminalCaps, file_uri, hyperlink
from aiden.tui.model import OK, ToolCall
from aiden.tui.render import render_cell
from aiden.tui.theme import Theme

# --------------------------------------------------------------------------- detection


def test_unknown_terminal_disables_hyperlinks():
    """Default off: a stray OSC 8 is garbage, a missing link is merely plain text."""
    caps = TerminalCaps.detect({})
    assert caps.hyperlinks is False


def test_known_terminals_are_detected():
    for program in ("Ghostty", "WezTerm", "iTerm.app", "kitty", "konsole"):
        caps = TerminalCaps.detect({"TERM_PROGRAM": program})
        assert caps.hyperlinks is True, program


def test_kitty_in_term_variable_is_recognised():
    assert TerminalCaps.detect({"TERM": "xterm-kitty"}).hyperlinks is True


def test_explicit_override_beats_detection_in_both_directions():
    # Force on for an unknown terminal...
    assert TerminalCaps.detect({"AIDEN_HYPERLINKS": "on"}).hyperlinks is True
    # ...and off for a known one.
    assert (
        TerminalCaps.detect({"TERM_PROGRAM": "Ghostty", "AIDEN_HYPERLINKS": "off"}).hyperlinks
        is False
    )


def test_sync_defaults_on_and_can_be_disabled():
    assert TerminalCaps.detect({}).sync is True
    assert TerminalCaps.detect({"AIDEN_SYNC": "0"}).sync is False


def test_truecolor_detection():
    assert TerminalCaps.detect({"COLORTERM": "truecolor"}).truecolor is True
    assert TerminalCaps.detect({"COLORTERM": "24bit"}).truecolor is True


def test_overrides_are_reported_for_diagnostics():
    caps = TerminalCaps.detect({"AIDEN_HYPERLINKS": "on", "AIDEN_SYNC": "0"})
    assert caps.overrides() == {"hyperlinks": True, "sync": False}
    assert "pinned" in caps.describe()


def test_garbage_override_value_is_ignored_rather_than_guessed():
    caps = TerminalCaps.detect({"AIDEN_HYPERLINKS": "maybe"})
    assert caps.hyperlinks is False, "an unparseable value must fall through to detection"


# --------------------------------------------------------------------------- hyperlinks


def test_hyperlink_wraps_only_when_supported():
    on = TerminalCaps(hyperlinks=True)
    off = TerminalCaps(hyperlinks=False)
    linked = hyperlink("aiden/tools/read.py", "file:///x/read.py", on)
    assert linked.startswith("\x1b]8;;file:///x/read.py")
    assert linked.endswith(OSC8_END)
    assert hyperlink("plain", "file:///x", off) == "plain"


def test_hyperlink_of_empty_input_is_a_no_op():
    on = TerminalCaps(hyperlinks=True)
    assert hyperlink("", "file:///x", on) == ""
    assert hyperlink("text", "", on) == "text"


def test_file_uri_is_absolute_and_optional_line():
    assert file_uri("/a/b.py") == "file:///a/b.py"
    assert file_uri("/a/b.py", 12) == "file:///a/b.py:12"


def test_relative_paths_become_absolute_uris():
    uri = file_uri("aiden/tools/read.py")
    assert uri.startswith("file:///")
    assert uri.endswith("aiden/tools/read.py")


# --------------------------------------------------------------------------- rendering


def _tool(output: str) -> ToolCall:
    return ToolCall(
        call_id="c1",
        name="grep",
        arguments={"pattern": "x", "path": "aiden/tools/read.py"},
        output=output,
        status=OK,
        duration_ms=1,
    )


def test_grep_output_paths_become_clickable_when_supported():
    theme = Theme(colour=False, ascii_mode=True)
    caps = TerminalCaps(hyperlinks=True)
    lines = render_cell(_tool("aiden/tools/read.py:94: some text"), 100, theme, caps).lines
    body = "\n".join(lines)
    assert "\x1b]8;;file://" in body
    assert "read.py:94" in body


def test_no_hyperlinks_by_default():
    """The same render with capabilities off must contain no OSC 8 at all."""
    theme = Theme(colour=False, ascii_mode=True)
    lines = render_cell(
        _tool("aiden/tools/read.py:94: some text"), 100, theme, TerminalCaps(hyperlinks=False)
    ).lines
    assert "\x1b]8;;" not in "\n".join(lines)


def test_the_tool_header_links_its_path_argument():
    theme = Theme(colour=False, ascii_mode=True)
    caps = TerminalCaps(hyperlinks=True)
    lines = render_cell(_tool("body"), 100, theme, caps).lines
    assert "\x1b]8;;file://" in lines[0]


def test_arbitrary_text_is_not_linked():
    """Only a leading path is linkable; linking arbitrary words would misfire."""
    theme = Theme(colour=False, ascii_mode=True)
    caps = TerminalCaps(hyperlinks=True)
    lines = render_cell(_tool("some prose without a path prefix\n"), 100, theme, caps).lines
    body = "\n".join(lines[1:])
    assert "\x1b]8;;" not in body
