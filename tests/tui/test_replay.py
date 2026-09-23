"""Replaying a session log through the renderer.

This is the architecture's central claim under test: the transcript is a projection of the log,
so a recorded run must reconstruct into the same event stream the driver rendered live.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from aiden import config
from aiden.events import (
    RunFinished,
    RunStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from aiden.session import (
    ENTRY_ASSISTANT,
    ENTRY_DIAGNOSTIC,
    ENTRY_RUN_END,
    ENTRY_TOOL_RESULT,
    ENTRY_USER,
    SessionStore,
)
from aiden.tui.driver import TUIDriver
from aiden.tui.replay import events_from_entries, events_from_session, questions_from_session
from aiden.tui.theme import Theme


@pytest.fixture
def log(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    store = SessionStore.create(cwd=Path("/repo"), model="opencode-go/qwen3.8-flash")
    store.append(ENTRY_USER, {"text": "what is the answer?"})
    store.append(
        ENTRY_ASSISTANT,
        {
            "text": "It is 42.",
            "thinking": "reading the file",
            "tool_calls": [{"id": "c1", "name": "read", "arguments": {"path": "notes.md"}}],
            "stop_reason": "tool_use",
        },
    )
    store.append(
        ENTRY_TOOL_RESULT,
        {
            "call_id": "c1",
            "name": "read",
            "is_error": False,
            "duration_ms": 3,
            "output": "the answer is 42",
        },
    )
    store.append(ENTRY_DIAGNOSTIC, {"message": "a cap left a trace", "level": "warning"})
    store.append(
        ENTRY_RUN_END,
        {
            "stop_reason": "end_turn",
            "turns": 2,
            "tool_calls": 1,
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "cost_usd": 0.0004,
            "answer": "It is 42.",
        },
    )
    store.close()
    return store.path


def test_replay_reconstructs_the_event_stream(log: Path):
    events = events_from_session(log)
    kinds = [type(e).__name__ for e in events]

    assert kinds[0] == "RunStarted"
    assert kinds[-1] == "RunFinished"
    assert "ToolCallStarted" in kinds
    assert "ToolCallFinished" in kinds
    assert "Diagnostic" in kinds

    started = next(e for e in events if isinstance(e, ToolCallStarted))
    assert started.name == "read"
    assert started.arguments == {"path": "notes.md"}

    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.output == "the answer is 42"
    assert finished.duration_ms == 3
    assert not finished.is_error

    end = next(e for e in events if isinstance(e, RunFinished))
    assert end.turns == 2
    assert end.cost_usd == pytest.approx(0.0004)
    assert end.usage.input_tokens == 100


def test_replay_preserves_the_question_and_model(log: Path):
    started = next(e for e in events_from_session(log) if isinstance(e, RunStarted))
    assert started.question == "what is the answer?"
    assert started.model == "opencode-go/qwen3.8-flash"


def test_replay_reconstructs_the_same_cells_a_live_run_would_produce(log: Path):
    """The claim under test: the log is a projection source, so replay rebuilds the transcript."""
    from aiden.tui.model import AssistantText, Notice, RunSummary, Thinking, ToolCall, UserPrompt

    driver = TUIDriver(out=io.StringIO(), theme=Theme(colour=False, ascii_mode=True), width=80)
    for event in events_from_session(log):
        driver.emit(event)

    cells = driver.transcript.cells
    # Reasoning, then prose, then the action — not the action before the prose that motivated it.
    assert [type(c).__name__ for c in cells] == [
        "UserPrompt",
        "Notice",
        "Thinking",
        "AssistantText",
        "ToolCall",
        "Notice",
        "RunSummary",
    ]

    assert isinstance(cells[0], UserPrompt) and cells[0].text == "what is the answer?"
    assert isinstance(cells[2], Thinking) and cells[2].text == "reading the file"
    assert isinstance(cells[3], AssistantText) and cells[3].text == "It is 42."
    tool = cells[4]
    assert isinstance(tool, ToolCall)
    assert tool.name == "read" and tool.arguments == {"path": "notes.md"}
    assert tool.output == "the answer is 42"
    assert isinstance(cells[5], Notice) and "cap left a trace" in cells[5].text
    summary = cells[6]
    assert isinstance(summary, RunSummary)
    assert summary.turns == 2 and summary.tool_calls == 1
    assert summary.usage.input_tokens == 100


def _usage(input_tokens: int, output_tokens: int):
    from aiden.providers.types import Usage

    return Usage(input_tokens=input_tokens, output_tokens=output_tokens)


def test_replay_of_a_multi_question_session(log: Path, monkeypatch, tmp_path: Path):
    """Each question begins its own run, matching how the live loop behaves."""
    store = SessionStore.create(cwd=Path("/repo"), model="m", session_id="multi")
    store.append(ENTRY_USER, {"text": "first"})
    store.append(ENTRY_ASSISTANT, {"text": "one", "stop_reason": "end_turn"})
    store.append(ENTRY_RUN_END, {"stop_reason": "end_turn", "turns": 1})
    store.append(ENTRY_USER, {"text": "second"})
    store.append(ENTRY_ASSISTANT, {"text": "two", "stop_reason": "end_turn"})
    store.append(ENTRY_RUN_END, {"stop_reason": "end_turn", "turns": 1})
    store.close()

    events = events_from_session(store.path)
    starts = [e for e in events if isinstance(e, RunStarted)]
    ends = [e for e in events if isinstance(e, RunFinished)]
    assert [s.question for s in starts] == ["first", "second"]
    assert len(ends) == 2


def test_questions_are_extracted_for_resume(log: Path):
    assert questions_from_session(log) == ["what is the answer?"]


def test_replay_tolerates_a_partial_log(tmp_path: Path, monkeypatch):
    """A crashed run leaves a truncated tail; replay must not explode."""
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    store = SessionStore.create(cwd=Path("/repo"), model="m")
    store.append(ENTRY_USER, {"text": "q"})
    store.close()
    with store.path.open("a") as fh:
        fh.write('{"type":"assistant_message","payload":{"text":"trunc')

    events = events_from_session(store.path)
    assert events, "the readable prefix must still replay"


def test_events_from_entries_handles_an_empty_log():
    assert events_from_entries([]) == []
