"""The reducer: cell boundaries and turn attribution.

These pin the bug where every turn's prose accumulated into a single cell, so a turn's answer
rendered above that turn's own marker and per-cell line accounting could not separate segments.
"""

from __future__ import annotations

from aiden.events import (
    Diagnostic,
    RunFinished,
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnStarted,
)
from aiden.providers.types import Usage
from aiden.tui.model import (
    ERROR,
    OK,
    RUNNING,
    AssistantText,
    Notice,
    RunSummary,
    Thinking,
    Transcript,
    TurnMarker,
    UserPrompt,
)


def feed(*events) -> Transcript:
    t = Transcript()
    for event in events:
        t.apply(event)
    t.finalize()
    return t


def test_run_start_adds_prompt_and_context_notice():
    t = feed(RunStarted(session_id="s", model="m", question="why?", cwd="/repo"))
    assert isinstance(t.cells[0], UserPrompt)
    assert t.cells[0].text == "why?"
    assert isinstance(t.cells[1], Notice)


def test_single_turn_has_no_turn_marker():
    """Chrome on every answer is noise; markers exist to separate turns."""
    t = feed(
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        TurnStarted(turn=1),
        TextDelta(text="answer"),
        RunFinished(stop_reason="end_turn", turns=1, tool_calls=0, usage=Usage(), cost_usd=0.0),
    )
    assert not t.of(TurnMarker)


def test_a_tool_call_closes_the_text_segment():
    """Text after a tool call is a new cell, not a continuation of the previous one."""
    t = feed(
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        TextDelta(text="before"),
        ToolCallStarted(call_id="c1", name="glob", arguments={}),
        ToolCallFinished(
            call_id="c1", name="glob", is_error=False, duration_ms=1, output_chars=0, output=""
        ),
        TextDelta(text="after"),
    )
    texts = [c.text for c in t.of(AssistantText)]
    assert texts == ["before", "after"], f"segments merged: {texts}"


def test_each_turn_gets_its_own_text_cell_in_order():
    t = feed(
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        TurnStarted(turn=1),
        TextDelta(text="first turn"),
        ToolCallStarted(call_id="c1", name="glob", arguments={}),
        ToolCallFinished(
            call_id="c1", name="glob", is_error=False, duration_ms=1, output_chars=0, output=""
        ),
        TurnStarted(turn=2),
        TextDelta(text="second turn"),
    )
    kinds = [type(c).__name__ for c in t.cells]
    # The marker for turn 2 must precede the text it introduces.
    assert kinds.index("TurnMarker") < len(kinds) - 1
    assert kinds[-1] == "AssistantText"
    assert [c.text for c in t.of(AssistantText)] == ["first turn", "second turn"]


def test_closed_text_cells_are_not_left_running():
    t = feed(
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        TextDelta(text="text"),
        ToolCallStarted(call_id="c1", name="glob", arguments={}),
    )
    assert t.of(AssistantText)[0].status == OK
    assert t.tail() is None


def test_open_cell_is_running_while_streaming():
    t = Transcript()
    t.apply(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    t.apply(TextDelta(text="partial"))
    assert t.tail() is not None
    assert t.tail().status == RUNNING


def test_thinking_accumulates_into_one_cell():
    t = feed(
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        ThinkingDelta(text="step one. "),
        ThinkingDelta(text="step two."),
    )
    cells = t.of(Thinking)
    assert len(cells) == 1
    assert cells[0].text == "step one. step two."


def test_tool_call_transitions_to_final_status():
    t = feed(
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        ToolCallStarted(call_id="c1", name="read", arguments={"path": "a"}),
        ToolCallFinished(
            call_id="c1", name="read", is_error=True, duration_ms=3, output_chars=9, output="nope"
        ),
    )
    call = t.tool_calls()[0]
    assert call.status == ERROR
    assert call.output == "nope"
    assert call.duration_ms == 3


def test_finish_without_start_still_records_the_result():
    """A recording that begins mid-turn must not lose a tool result."""
    t = feed(
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        ToolCallFinished(
            call_id="orphan",
            name="grep",
            is_error=False,
            duration_ms=1,
            output_chars=2,
            output="ok",
        ),
    )
    assert t.tool_calls()[0].call_id == "orphan"
    assert t.tool_calls()[0].output == "ok"


def test_diagnostics_become_notices_with_their_level():
    t = feed(
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        Diagnostic(message="budget exhausted", level="error"),
    )
    notice = next(c for c in t.of(Notice) if c.text == "budget exhausted")
    assert notice.level == "error"


def test_run_finished_adds_a_summary_with_the_numbers():
    t = feed(
        RunStarted(session_id="s", model="m", question="q", cwd="/repo"),
        RunFinished(
            stop_reason="end_turn",
            turns=3,
            tool_calls=4,
            usage=Usage(input_tokens=10, output_tokens=5),
            cost_usd=0.0012,
        ),
    )
    summary = t.of(RunSummary)[0]
    assert summary.turns == 3 and summary.tool_calls == 4
    assert summary.cost_usd == 0.0012


def test_finalize_closes_the_open_cell():
    t = Transcript()
    t.apply(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    t.apply(TextDelta(text="final text"))
    t.finalize()
    assert t.tail() is None
    # `committed` is the driver's render state ("already written to scrollback"), not the
    # reducer's. The reducer setting it made the driver think the cell was fully emitted, so
    # finalized text never reached the transcript.
    assert t.of(AssistantText)[0].committed == ""
