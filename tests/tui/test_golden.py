"""Golden-frame snapshots at the three mandated widths.

research/06 §What great agent UIs do #17: "If your UI can't be snapshotted, it will regress."
Codex snapshot-tests ``diff_gallery_80x24 / 94x35 / 120x40``; these are those snapshots.

Run ``AIDEN_UPDATE_GOLDEN=1 uv run python -m pytest tests/tui/test_golden.py`` to rewrite them.
"""

from __future__ import annotations

import pytest

from aiden.providers.types import Usage
from aiden.tui import golden, render
from aiden.tui.model import Transcript
from aiden.tui.theme import Theme

from .conftest import long_tool_output, realistic_turn

WIDTHS = [80, 94, 120]


@pytest.mark.parametrize("width", WIDTHS)
def test_full_turn_frame_matches_golden(width: int, theme: Theme):
    lines = golden.frame_from_events(realistic_turn(), width, theme)
    assert golden.compare_or_update(f"turn.{width}x", width, lines) is None


@pytest.mark.parametrize("width", WIDTHS)
def test_plain_mode_frame_matches_golden(width: int, theme: Theme):
    """The plain renderer gets its own snapshot: it must stay linear and complete."""
    transcript = golden.transcript_from_events(realistic_turn())
    from aiden.tui import plain

    lines = plain.render_all(transcript.cells, width, theme)
    assert golden.compare_or_update(f"plain.{width}x", width, lines) is None


def test_golden_frames_are_stable_across_repeat_renderings(theme: Theme):
    """Rendering must be deterministic, or no snapshot means anything."""
    first = golden.frame_from_events(realistic_turn(), 94, theme)
    second = golden.frame_from_events(realistic_turn(), 94, theme)
    assert first == second


def test_every_line_fits_the_frame_width(theme: Theme):
    """An overlong line is a rendering bug: it would wrap and corrupt the damage rect."""
    for width in WIDTHS:
        for line in golden.frame_from_events(realistic_turn(), width, theme):
            assert len(line) <= width, f"{line!r} exceeds {width}"


def test_long_tool_output_is_elided_with_a_count(theme: Theme):
    """Spec: visible lines capped, with '… N more (e)'. The full text stays out of the render path."""
    lines = golden.frame_from_events(long_tool_output(40), 94, theme)
    body = "\n".join(lines)
    assert "more (e to expand)" in body
    assert "match 39" not in body  # the tail is not rendered
    assert "match 0" in body


def test_expanding_a_tool_call_shows_more_lines(theme: Theme):
    events = long_tool_output(40)
    transcript = Transcript()
    for event in events:
        transcript.apply(event)
    transcript.finalize()

    call = transcript.tool_calls()[0]
    collapsed = render.render_cell(call, 94, theme).lines
    call.expanded = True
    expanded = render.render_cell(call, 94, theme).lines

    assert len(expanded) > len(collapsed)
    assert "match 39" in "\n".join(expanded)


def test_unknown_cost_is_not_shown_as_zero(theme: Theme):
    """The HUD must never lie (research/06 §Microcopy)."""
    from aiden.events import RunFinished
    from aiden.tui.model import RunSummary

    summary = RunSummary(stop_reason="end_turn", turns=1, tool_calls=0, cost_usd=0.0)
    assert "cost —" in "\n".join(render.render_cell(summary, 94, theme).lines)

    _ = RunFinished  # keep the import meaningful for the type checker


def test_ascii_theme_uses_ascii_glyphs(theme: Theme):
    transcript = golden.transcript_from_events(realistic_turn())
    lines = render.render_all(transcript.cells, 94, theme)
    body = "\n".join(lines)
    assert "✓" not in body and "▌" not in body and "…" not in body
    assert "[ok]" in body  # the ASCII ok glyph


def test_colour_theme_emits_escapes(colour_theme: Theme):
    """Colour mode is exercised too, so the style plumbing cannot rot silently."""
    lines = render.markdown_block("# Title\n\nSome **bold** text.", 60, colour_theme)
    assert any("\x1b[" in line for line in lines)


def test_markdown_block_wraps_to_width(colour_theme: Theme):
    lines = render.markdown_block("word " * 40, 40, colour_theme)
    assert len(lines) > 1
    for line in lines:
        assert len(line) <= 60  # rich counts escapes; just bound the visible length loosely


def test_thinking_is_folded_by_default(theme: Theme):
    transcript = golden.transcript_from_events(realistic_turn())
    thinking = transcript.of(__import__("aiden.tui.model", fromlist=["Thinking"]).Thinking)[0]
    collapsed = render.render_cell(thinking, 94, theme).lines
    assert len(collapsed) == 1
    assert "expand" in collapsed[0]

    thinking.collapsed = False
    assert len(render.render_cell(thinking, 94, theme).lines) > 1


def test_long_prose_wraps_instead_of_being_clipped(theme: Theme):
    """Regression: prose was run through the clipping path, so answers ended mid-sentence.

    Assistant text arrives as one long line per markdown block, so clipping mangled every
    answer that exceeded the terminal width.
    """
    from aiden.events import RunFinished, RunStarted, TextDelta
    from aiden.tui import golden

    paragraph = " ".join(["wording"] * 60)
    events = [
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        TextDelta(text=paragraph),
        RunFinished(stop_reason="end_turn", turns=1, tool_calls=0, usage=Usage(), cost_usd=0.0),
    ]
    lines = golden.frame_from_events(events, 80, theme)
    body = [line for line in lines if line.strip()]

    # Nothing may be dropped or elided: every word survives across the wrapped lines.
    assert "wording" * 1 in " ".join(body)
    joined = " ".join(body)
    assert joined.count("wording") == 60, "wrapping must not lose text"
    assert "…" not in joined, "prose must wrap, not clip"
    for line in lines:
        assert len(line) <= 80


def test_wrapping_preserves_indentation(theme: Theme):
    from aiden.tui.render import plain_lines

    lines = plain_lines("  indented " * 20, 40, theme)
    assert len(lines) > 1
    assert all(line.startswith("  ") for line in lines)


def test_tool_output_is_not_a_silent_preview(theme: Theme):
    """A short preview presented as the whole result is the silent truncation the spec forbids."""
    from aiden.tui.model import OK, ToolCall
    from aiden.tui.render import render_cell

    cell = ToolCall(
        call_id="c1",
        name="read",
        status=OK,
        output="\n".join(f"line {i}" for i in range(30)),
        duration_ms=1,
    )
    body = "\n".join(render_cell(cell, 80, theme).lines)
    assert "more (e to expand)" in body, "hidden lines must be counted, not hidden silently"


def test_tool_output_wraps_so_code_stays_readable(theme: Theme):
    """A clipped line of file contents tells you a line is missing but not what it said."""
    from aiden.tui.model import OK, ToolCall
    from aiden.tui.render import render_cell

    long_line = "def a_function_with_a_long_name(argument_one, argument_two, argument_three):"
    cell = ToolCall(call_id="c1", name="read", status=OK, output=long_line, duration_ms=1)
    lines = render_cell(cell, 60, theme).lines

    body = " ".join(line.strip() for line in lines[1:])
    assert "argument_three" in body, "the tail of the line must survive"
    assert all(len(line) <= 60 for line in lines)
    # every wrapped piece keeps the two-column gutter
    assert all(line.startswith("  ") for line in lines[1:])


def test_tool_output_blank_lines_stay_blank(theme: Theme):
    from aiden.tui.model import OK, ToolCall
    from aiden.tui.render import render_cell

    cell = ToolCall(call_id="c1", name="read", status=OK, output="a\n\nb", duration_ms=1)
    lines = render_cell(cell, 40, theme).lines
    assert "" in lines[1:], "blank lines must not become whitespace-padded gutters"


def test_notices_wrap_instead_of_being_clipped(theme: Theme):
    """A wrapped-up instruction that is cut off is worse than useless."""
    from aiden.tui.model import Notice
    from aiden.tui.render import render_cell

    text = "You have 3 turns left before the run is cut off. Stop searching and answer now."
    lines = render_cell(Notice(text=text, level="info"), 46, theme).lines
    assert len(lines) > 1
    assert "answer now" in " ".join(lines)
    assert all(len(line) <= 46 for line in lines)


def test_long_session_path_keeps_the_filename_visible(theme: Theme):
    from aiden.tui.model import RunSummary
    from aiden.tui.render import render_cell

    summary = RunSummary(
        stop_reason="end_turn",
        turns=1,
        tool_calls=0,
        cost_usd=0.001,
        session_path="/Users/someone/.aiden/sessions/--very-long-project-name--/20260101T000000-abcdef01.jsonl",
    )
    body = "\n".join(render_cell(summary, 50, theme).lines)
    assert "abcdef01.jsonl" in body, "the filename must survive elision"


def test_cell_renderers_actually_emit_colour(colour_theme: Theme):
    """The gap that let a fully monochrome UI ship: only the markdown path was colour-tested.

    The cell renderers called `theme.style(...)`, embedded it in a `rich.Text`, and then read
    `.plain`, which strips the style. Every cell rendered unstyled while the token table looked
    correct and `markdown_block` (rendered by rich) passed its colour test.
    """
    from aiden.events import (
        Diagnostic,
        RunFinished,
        RunStarted,
        TextDelta,
        ToolCallFinished,
        ToolCallStarted,
    )
    from aiden.tui import golden

    events = [
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        TextDelta(text="an answer\n"),
        ToolCallStarted(call_id="c1", name="read", arguments={"path": "a.py"}),
        ToolCallFinished(
            call_id="c1",
            name="read",
            is_error=False,
            duration_ms=1,
            output_chars=5,
            output="body",
        ),
        Diagnostic(message="a warning", level="warning"),
        RunFinished(stop_reason="end_turn", turns=1, tool_calls=1, usage=Usage(), cost_usd=0.001),
    ]
    lines = golden.frame_from_events(events, 80, colour_theme)
    styled = [line for line in lines if "\x1b[" in line]

    assert len(styled) >= 5, f"expected most cells to be styled, got {len(styled)}"
    # Distinct tokens must produce distinct codes, or the palette is decorative.
    codes = {line.split("m", 1)[0] for line in styled}
    assert len(codes) > 2, f"styling collapsed to {codes}"


def test_no_colour_mode_emits_no_escapes_at_all(theme: Theme):
    """NO_COLOR must yield byte-clean text: golden diffs and screen readers depend on it."""
    from aiden.events import Diagnostic, RunFinished, RunStarted, TextDelta, ToolCallStarted
    from aiden.tui import golden

    events = [
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        TextDelta(text="text\n"),
        ToolCallStarted(call_id="c1", name="glob", arguments={}),
        Diagnostic(message="warning", level="warning"),
        RunFinished(stop_reason="end_turn", turns=1, tool_calls=0, usage=Usage(), cost_usd=0.0),
    ]
    for line in golden.frame_from_events(events, 80, theme):
        assert "\x1b" not in line, f"escape leaked into a plain frame: {line!r}"
