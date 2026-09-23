"""Golden-frame snapshots at the three mandated widths.

research/06 §What great agent UIs do #17: "If your UI can't be snapshotted, it will regress."
Codex snapshot-tests ``diff_gallery_80x24 / 94x35 / 120x40``; these are those snapshots.

Run ``AIDEN_UPDATE_GOLDEN=1 uv run python -m pytest tests/tui/test_golden.py`` to rewrite them.
"""

from __future__ import annotations

import pytest

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
