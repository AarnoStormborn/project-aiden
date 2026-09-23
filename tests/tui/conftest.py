"""Shared TUI test fixtures and helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiden.events import (
    Diagnostic,
    RunFinished,
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnStarted,
    UsageUpdated,
)
from aiden.providers.types import Usage
from aiden.tui.theme import Theme


@pytest.fixture
def theme() -> Theme:
    """Deterministic theme: dark, no colour, ASCII glyphs.

    Golden frames must not depend on the developer's terminal, `NO_COLOR`, or locale, and
    escape sequences make a diff unreadable. Layout and structure are what the snapshot is for,
    so colour is off and ASCII glyphs are pinned.
    """
    return Theme(dark=True, colour=False, ascii_mode=True)


@pytest.fixture
def colour_theme() -> Theme:
    return Theme(dark=True, colour=True, ascii_mode=False)


def realistic_turn() -> list:
    """A turn with every cell type: the fixture the golden frames are built from.

    Kept as a function rather than a JSON file so the events stay type-checked; the recorded
    JSONL path is exercised separately in ``test_golden.py``.
    """
    return [
        RunStarted(
            session_id="20260101T000000-abcdef01",
            model="opencode-go/qwen3.8-flash",
            question="What are the tool output budgets?",
            cwd="/repo/project-aiden",
        ),
        TurnStarted(turn=1),
        ThinkingDelta(text="The budgets live in config.py. Let me read it."),
        TextDelta(text="I'll read the config.\n"),
        ToolCallStarted(call_id="c1", name="read", arguments={"path": "aiden/config.py"}),
        ToolCallFinished(
            call_id="c1",
            name="read",
            is_error=False,
            duration_ms=1,
            output_chars=120,
            output="READ_MAX_BYTES = 16_384",
        ),
        UsageUpdated(usage=Usage(input_tokens=900, output_tokens=40), cost_usd=0.0002),
        TurnStarted(turn=2),
        TextDelta(text="The budgets are:\n"),
        TurnStarted(turn=3),
        ToolCallStarted(call_id="c2", name="grep", arguments={"pattern": "TODO" * 12}),
        ToolCallFinished(
            call_id="c2",
            name="grep",
            is_error=True,
            duration_ms=11,
            output_chars=48,
            output="no matches for /TODO/ in .",
        ),
        Diagnostic(message="output budget exhausted by reasoning", level="warning"),
        TextDelta(text="\nREAD_MAX_BYTES is 16_384 and READ_MAX_LINES is 400.\n"),
        RunFinished(
            stop_reason="end_turn",
            turns=3,
            tool_calls=2,
            usage=Usage(input_tokens=1_800, output_tokens=120, cache_read_tokens=64),
            cost_usd=0.00042,
            session_path="/home/u/.aiden/sessions/--repo-project-aiden--/20260101T000000-abcdef01.jsonl",
            answer="READ_MAX_BYTES is 16_384.",
        ),
    ]


def long_tool_output(lines: int = 40) -> list:
    events = [
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        ToolCallStarted(call_id="c1", name="grep", arguments={"pattern": "x"}),
    ]
    events.append(
        ToolCallFinished(
            call_id="c1",
            name="grep",
            is_error=False,
            duration_ms=5,
            output_chars=lines * 10,
            output="\n".join(f"match {i}" for i in range(lines)),
        )
    )
    events.append(
        RunFinished(stop_reason="end_turn", turns=1, tool_calls=1, usage=Usage(), cost_usd=0.0)
    )
    return events


GOLDEN_DIR = Path(__file__).parent / "golden"
