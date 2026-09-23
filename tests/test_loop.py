"""The loop state machine, driven by a scripted fake provider.

These tests are about *policy*, not transport: what the loop does with a text-only answer, a
tool call, a truncated call, an error, and the two ceilings. Using a fake suite keeps them
fast and network-free while still exercising session writes and event emission.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aiden import config
from aiden.events import (
    Diagnostic,
    RecordingSink,
    RunFinished,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
)
from aiden.loop import run_loop
from aiden.providers.types import Completion, Cost, ModelInfo, ToolCallPart, Usage
from aiden.session import (
    ENTRY_ASSISTANT,
    ENTRY_RUN_END,
    ENTRY_TOOL_RESULT,
    ENTRY_USAGE,
    ENTRY_USER,
    SessionStore,
    read_entries,
)


class FakeSuite:
    """Returns pre-scripted completions in order, recording what it was asked."""

    def __init__(self, completions: list[Completion]):
        self._completions = list(completions)
        self.calls: list[list[Any]] = []

    def resolve(self, ref: str) -> ModelInfo:
        return ModelInfo(
            id="fake",
            provider="fake",
            api="anthropic-messages",
            base_url="http://localhost",
            name="Fake",
            cost=Cost(input=1.0, output=1.0),
        )

    async def complete(self, model, messages, tools=None, **kw) -> Completion:
        self.calls.append(list(messages))
        if not self._completions:
            raise AssertionError("loop asked for more turns than the script provides")
        result = self._completions.pop(0)
        # Mirror a real transport: cost is derived from the model's price table, not supplied
        # by the caller, so cost-ceiling behaviour is exercised realistically.
        result.cost_usd = model.cost.of(result.usage)
        return result


def completion(
    *,
    text: str = "",
    calls: list[ToolCallPart] | None = None,
    stop: str = "end_turn",
    usage: Usage | None = None,
    diagnostic: str = "",
) -> Completion:
    return Completion(
        text=text,
        tool_calls=calls or [],
        stop_reason=stop,  # type: ignore[arg-type]
        usage=usage or Usage(input_tokens=10, output_tokens=5),
        cost_usd=0.0001,
        diagnostic=diagnostic,
    )


@pytest.fixture
def isolated_sessions(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config, "SPILL_DIR", tmp_path / "spill")
    return tmp_path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "notes.md").write_text("# Notes\n\nThe answer is 42.\n")
    return root


# --------------------------------------------------------------------------- happy paths


async def test_text_only_answer_finishes_in_one_turn(isolated_sessions, project):
    suite = FakeSuite([completion(text="The answer is 42.")])
    sink = RecordingSink()

    result = await run_loop(
        "what is the answer?", suite=suite, model="fake", cwd=project, sink=sink
    )

    assert result.answer == "The answer is 42."
    assert result.turns == 1
    assert result.tool_calls == 0
    assert result.stop_reason == "end_turn"
    assert result.ok
    assert sink.text == "The answer is 42."


async def test_events_are_emitted_in_order(isolated_sessions, project):
    suite = FakeSuite([completion(text="hi")])
    sink = RecordingSink()
    await run_loop("q", suite=suite, model="fake", cwd=project, sink=sink)

    kinds = [type(e).__name__ for e in sink.events]
    assert kinds[0] == "RunStarted"
    assert kinds[1] == "TurnStarted"
    assert "TextDelta" in kinds
    assert kinds[-1] == "RunFinished"
    assert sink.of(RunFinished)[0].answer == "hi"


async def test_tool_call_is_executed_and_result_fed_back(isolated_sessions, project):
    suite = FakeSuite(
        [
            completion(
                calls=[ToolCallPart(id="c1", name="read", arguments={"path": "notes.md"})],
                stop="tool_use",
            ),
            completion(text="It says the answer is 42."),
        ]
    )
    sink = RecordingSink()

    result = await run_loop(
        "what does notes.md say?", suite=suite, model="fake", cwd=project, sink=sink
    )

    assert result.answer.startswith("It says")
    assert result.turns == 2
    assert result.tool_calls == 1

    # The tool actually ran and its output reached the second request.
    second_request = suite.calls[1]
    tool_messages = list(m for m in second_request if m.role == "tool")
    assert tool_messages, "the tool result was not sent back to the model"
    first_part = tool_messages[0].content[0]
    assert "answer is 42" in first_part.output  # type: ignore[union-attr]

    started = sink.of(ToolCallStarted)[0]
    finished = sink.of(ToolCallFinished)[0]
    assert started.name == "read"
    assert finished.name == "read"
    assert not finished.is_error
    assert finished.output_chars > 0


async def test_results_are_returned_in_call_order(isolated_sessions, project):
    """Determinism: results must match call order even if a tool were slow."""

    suite = FakeSuite(
        [
            completion(
                calls=[
                    ToolCallPart(id="a", name="read", arguments={"path": "notes.md"}),
                    ToolCallPart(id="b", name="glob", arguments={"pattern": "*.md"}),
                ],
                stop="tool_use",
            ),
            completion(text="done"),
        ]
    )
    await run_loop("q", suite=suite, model="fake", cwd=project)

    tool_message = next(m for m in suite.calls[1] if m.role == "tool")
    assert [p.call_id for p in tool_message.content] == ["a", "b"]


# --------------------------------------------------------------------------- caps / errors


async def test_truncated_tool_call_is_never_executed(isolated_sessions, project):
    """Rule 1: partial streamed arguments must fail, not run."""
    suite = FakeSuite(
        [
            completion(
                calls=[
                    ToolCallPart(
                        id="c1",
                        name="read",
                        arguments={},
                        raw_arguments='{"path":"notes',
                        truncated=True,
                    )
                ],
                stop="max_tokens",
            ),
            completion(text="I could not read it."),
        ]
    )
    sink = RecordingSink()

    await run_loop("read it", suite=suite, model="fake", cwd=project, sink=sink)

    # The call was skipped, and the model was told why.
    finished = sink.of(ToolCallFinished)[0]
    assert finished.skipped
    assert finished.is_error
    assert any("not executed" in d.message for d in sink.of(Diagnostic))

    tool_message = next(m for m in suite.calls[1] if m.role == "tool")
    assert "truncated" in tool_message.content[0].output  # type: ignore[union-attr]


async def test_provider_error_stops_the_loop_and_is_reported(isolated_sessions, project):
    suite = FakeSuite([completion(text="", stop="error", diagnostic="rate limited, try later")])
    sink = RecordingSink()

    result = await run_loop("q", suite=suite, model="fake", cwd=project, sink=sink)

    assert not result.ok
    assert result.stop_reason == "error"
    assert "rate limited" in result.error
    assert sink.of(RunFinished)[0].error


async def test_turn_ceiling_stops_a_looping_agent(isolated_sessions, project):
    """A tool-calling loop must terminate and say why."""
    forever = [
        completion(
            calls=[ToolCallPart(id=f"c{i}", name="read", arguments={"path": "notes.md"})],
            stop="tool_use",
        )
        for i in range(10)
    ]
    suite = FakeSuite(forever)
    sink = RecordingSink()

    result = await run_loop(
        "loop forever", suite=suite, model="fake", cwd=project, sink=sink, max_turns=3
    )

    assert result.turns == 3
    assert result.stop_reason == "max_turns"
    assert any("turn ceiling" in d for d in result.diagnostics)
    assert sink.of(Diagnostic)


async def test_cost_ceiling_stops_before_the_next_turn(isolated_sessions, project):
    expensive = [
        completion(
            calls=[ToolCallPart(id="c1", name="read", arguments={"path": "notes.md"})],
            stop="tool_use",
            usage=Usage(input_tokens=1_000_000, output_tokens=1_000_000),
        )
    ]
    suite = FakeSuite(expensive)
    sink = RecordingSink()

    result = await run_loop(
        "spend money", suite=suite, model="fake", cwd=project, sink=sink, max_cost_usd=0.01
    )

    assert result.stop_reason == "cost_limit"
    assert result.turns == 1
    assert any("cost ceiling" in d for d in result.diagnostics)


async def test_provider_diagnostic_is_surfaced(isolated_sessions, project):
    """A cap must leave a trace (research/02 §3)."""
    suite = FakeSuite([completion(text="pong", diagnostic="output budget exhausted by reasoning")])
    sink = RecordingSink()

    result = await run_loop("q", suite=suite, model="fake", cwd=project, sink=sink)

    assert any("reasoning" in d for d in result.diagnostics)
    assert any("reasoning" in d.message for d in sink.of(Diagnostic))


async def test_unknown_tool_does_not_kill_the_run(isolated_sessions, project):
    suite = FakeSuite(
        [
            completion(calls=[ToolCallPart(id="c1", name="rm_rf", arguments={})], stop="tool_use"),
            completion(text="I do not have that tool."),
        ]
    )
    sink = RecordingSink()

    result = await run_loop("delete everything", suite=suite, model="fake", cwd=project, sink=sink)

    assert result.answer.startswith("I do not have")
    assert sink.of(ToolCallFinished)[0].is_error


# --------------------------------------------------------------------------- accounting


async def test_usage_and_cost_accumulate_across_turns(isolated_sessions, project):
    suite = FakeSuite(
        [
            completion(
                calls=[ToolCallPart(id="c1", name="read", arguments={"path": "notes.md"})],
                stop="tool_use",
                usage=Usage(input_tokens=100, output_tokens=20, cache_read_tokens=5),
            ),
            completion(text="done", usage=Usage(input_tokens=50, output_tokens=10)),
        ]
    )
    sink = RecordingSink()

    result = await run_loop("q", suite=suite, model="fake", cwd=project, sink=sink)

    assert result.usage.input_tokens == 150
    assert result.usage.output_tokens == 30
    assert result.usage.cache_read_tokens == 5
    # (100+20) + (50+10) tokens at $1/M input and output; cache reads are unpriced here.
    assert result.cost_usd == pytest.approx(0.00018)
    assert sink.of(UsageUpdated)[-1].cost_usd == pytest.approx(0.00018)


# --------------------------------------------------------------------------- session log


async def test_session_records_the_whole_run(isolated_sessions, project):
    suite = FakeSuite(
        [
            completion(
                calls=[ToolCallPart(id="c1", name="read", arguments={"path": "notes.md"})],
                stop="tool_use",
            ),
            completion(text="the answer is 42"),
        ]
    )
    result = await run_loop("what is the answer?", suite=suite, model="fake", cwd=project)

    entries = list(read_entries(Path(result.session_path)))
    types = [e.type for e in entries]
    assert types[0] == "session"
    assert ENTRY_USER in types
    assert ENTRY_ASSISTANT in types
    assert ENTRY_TOOL_RESULT in types
    assert ENTRY_USAGE in types
    assert types[-1] == ENTRY_RUN_END

    user_entry = next(e for e in entries if e.type == ENTRY_USER)
    assert user_entry.payload["text"] == "what is the answer?"
    # The prompt hash makes a behaviour change attributable to a prompt change.
    assert user_entry.payload["system_prompt_hash"]

    end = entries[-1].payload
    assert end["answer"] == "the answer is 42"
    assert end["turns"] == 2


async def test_session_is_created_outside_the_project(isolated_sessions, project):
    suite = FakeSuite([completion(text="ok")])
    result = await run_loop("q", suite=suite, model="fake", cwd=project)
    assert str(project) not in result.session_path


async def test_provided_session_is_reused_not_recreated(isolated_sessions, project):
    store = SessionStore.create(cwd=project, model="fake")
    suite = FakeSuite([completion(text="ok")])

    result = await run_loop("q", suite=suite, model="fake", cwd=project, session=store)

    assert result.session_path == str(store.path)
    # run_loop must not close a session it does not own.
    assert store._fh is None or not store._fh.closed
    store.close()


async def test_run_end_is_written_even_when_the_provider_fails(isolated_sessions, project):
    suite = FakeSuite([completion(stop="error", diagnostic="boom")])
    result = await run_loop("q", suite=suite, model="fake", cwd=project)

    entries = list(read_entries(Path(result.session_path)))
    assert entries[-1].type == ENTRY_RUN_END
    assert entries[-1].payload["stop_reason"] == "error"


async def test_wrap_up_notice_is_injected_near_the_turn_ceiling(isolated_sessions, project):
    """Measured need: the agent had the answer by turn 5 and still hit the ceiling."""
    tool_turn = lambda i: completion(  # noqa: E731
        calls=[ToolCallPart(id=f"c{i}", name="read", arguments={"path": "notes.md"})],
        stop="tool_use",
    )
    suite = FakeSuite([tool_turn(i) for i in range(5)])
    sink = RecordingSink()

    await run_loop("q", suite=suite, model="fake", cwd=project, sink=sink, max_turns=5)

    # By turn 3 (5 - 2 used) the harness must have told the model to wrap up.
    final_request = suite.calls[-1]
    notices = [
        p.text
        for m in final_request
        for p in m.content
        if hasattr(p, "text") and "[harness]" in p.text
    ]
    assert notices, "no wrap-up notice was injected"
    assert "turn" in notices[-1] and "left" in notices[-1]

    # The notice is recorded, so the transcript explains why the agent stopped looking.
    assert any("[harness]" in d.message for d in sink.of(Diagnostic))


async def test_no_wrap_up_notice_when_plenty_of_turns_remain(isolated_sessions, project):
    suite = FakeSuite(
        [
            completion(
                calls=[ToolCallPart(id="c1", name="read", arguments={"path": "notes.md"})],
                stop="tool_use",
            ),
            completion(text="done"),
        ]
    )
    await run_loop("q", suite=suite, model="fake", cwd=project, max_turns=12)
    assert all(
        "[harness]" not in getattr(p, "text", "") for m in suite.calls[-1] for p in m.content
    )


async def test_wrap_up_notice_does_not_fire_on_the_first_turn_of_a_short_run(
    isolated_sessions, project
):
    """Regression: a fixed threshold equal to the whole budget nudged on turn 1.

    With max_turns=3 the agent was told to wrap up before reading anything, and answered a
    question it could have answered with one more tool call.
    """
    tool_turn = completion(
        calls=[ToolCallPart(id="c1", name="read", arguments={"path": "notes.md"})],
        stop="tool_use",
    )
    suite = FakeSuite([tool_turn, tool_turn, completion(text="answered")])
    sink = RecordingSink()

    await run_loop("q", suite=suite, model="fake", cwd=project, sink=sink, max_turns=3)

    first_request = suite.calls[0]
    assert not any(
        "[harness]" in getattr(p, "text", "") for m in first_request for p in m.content
    ), "the wrap-up notice must not fire before any work has happened"
    # It still fires by the final turn, so the run cannot die without an answer.
    assert any("[harness]" in d.message for d in sink.of(Diagnostic))


async def test_wrap_up_threshold_scales_down_for_short_budgets(isolated_sessions, project):
    """A 1-turn budget cannot nudge at all: there is no earlier turn to nudge from."""
    suite = FakeSuite(
        [
            completion(
                calls=[ToolCallPart(id="c1", name="read", arguments={"path": "notes.md"})],
                stop="tool_use",
            )
        ]
    )
    sink = RecordingSink()
    await run_loop("q", suite=suite, model="fake", cwd=project, sink=sink, max_turns=1)
    nudges = [d for d in sink.of(Diagnostic) if "[harness]" in d.message]
    assert not nudges, "a single-turn run has no room for a nudge"


async def test_an_interrupted_run_is_recorded_as_aborted(isolated_sessions, project):
    """Regression: a Ctrl-C'd run logged `end_turn`, so a replay showed no sign of the interrupt."""
    import asyncio

    class InterruptingSuite(FakeSuite):
        async def complete(self, model, messages, tools=None, **kw):
            raise asyncio.CancelledError

    suite = InterruptingSuite([])
    with pytest.raises(asyncio.CancelledError):
        await run_loop("q", suite=suite, model="fake", cwd=project)

    logs = list((isolated_sessions / "sessions").rglob("*.jsonl"))
    assert logs, "the run should still have written a log"
    entries = list(read_entries(logs[0]))
    end = entries[-1]
    assert end.type == ENTRY_RUN_END
    assert end.payload["stop_reason"] == "aborted"


async def test_a_keyboard_interrupt_is_also_recorded_as_aborted(isolated_sessions, project):
    class InterruptingSuite(FakeSuite):
        async def complete(self, model, messages, tools=None, **kw):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await run_loop("q", suite=InterruptingSuite([]), model="fake", cwd=project)

    logs = list((isolated_sessions / "sessions").rglob("*.jsonl"))
    entries = list(read_entries(logs[0]))
    assert entries[-1].payload["stop_reason"] == "aborted"
