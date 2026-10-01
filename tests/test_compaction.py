"""Compaction: the trigger, the pair-safe cut, and what the loop does with it.

The loop-level tests here assert on what the *model was sent* after compaction, not on a helper's
return value. That is deliberate: every wiring bug found in this repository so far — colour,
markdown, hyperlinks, `/undo`, the nesting marker — was machinery that worked in isolation and was
never reached in a real run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aiden import config
from aiden.context import estimate_tokens, should_compact
from aiden.context.compact import (
    apply_compaction,
    cut_index,
    mechanical_note,
    needs_compaction,
    render_for_summary,
    summarise,
    summary_prompt,
)
from aiden.loop import run_loop
from aiden.providers.types import (
    Completion,
    Cost,
    Message,
    ModelInfo,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
)
from aiden.session import ENTRY_COMPACTION, SessionStore, read_entries


def call(call_id: str, name: str = "read", **arguments: Any) -> ToolCallPart:
    return ToolCallPart(
        id=call_id, name=name, arguments=arguments, raw_arguments=json.dumps(arguments)
    )


def result(call_id: str, output: str) -> ToolResultPart:
    return ToolResultPart(call_id=call_id, output=output)


# --------------------------------------------------------------------------- estimator


def test_the_estimator_rounds_up_rather_than_down():
    """Under-counting is the dangerous direction: it means the request overflows instead."""
    assert estimate_tokens([Message.text("user", "x" * 4)]) > 0
    # 1 char is 1 token, not 0, so a request is never estimated as free.
    assert estimate_tokens([Message.text("user", "x")]) > 1


def test_every_message_pays_its_overhead():
    """Role and markup tokens carry no characters, so a pile of empty messages is not free."""
    empty = [Message(role="user", content=[]) for _ in range(10)]
    assert estimate_tokens(empty) >= 10 * 4


def test_tool_arguments_and_results_are_counted():
    """The two biggest things in a coding transcript; ignoring either makes the estimate useless."""
    small = estimate_tokens([Message.text("user", "hello")])
    with_args = estimate_tokens([Message(role="assistant", content=[call("1", path="x" * 400)])])
    with_result = estimate_tokens([Message(role="tool", content=[result("1", "y" * 4000)])])
    assert with_args > small
    assert with_result >= 1000


def test_an_unknown_window_never_triggers_compaction():
    """A catalog entry without a contextWindow must not make every run compact on turn one."""
    assert should_compact(10**9, context_window=0) is False


def test_the_reserve_is_what_triggers_it():
    window = 128_000
    # The reserve is the reply's room: without it the request fits and the *answer* is what overflows.
    assert should_compact(window - config.RESERVE_TOKENS - 1, window) is False
    assert should_compact(window - config.RESERVE_TOKENS + 1, window) is True


# --------------------------------------------------------------------------- cut point


def conversation() -> list[Message]:
    return [
        Message.text("user", "the original question"),
        Message(role="assistant", content=[call("1")]),
        Message(role="tool", content=[result("1", "A" * 4000)]),
        Message.text("user", "a follow up"),
        Message(role="assistant", content=[call("2")]),
        Message(role="tool", content=[result("2", "B" * 4000)]),
    ]


@pytest.mark.parametrize("budget", [1, 50, 500, 1000, 2000, 3000, 5000])
def test_the_cut_never_splits_a_call_from_its_result(budget: int):
    """A cut landing on a tool message leaves an orphaned result, and the request is rejected.

    Compaction would then break the run it was rescuing, which is the worst possible failure for a
    recovery mechanism.
    """
    messages = conversation()
    cut = cut_index(messages, tail_budget_tokens=budget)
    if cut < len(messages):
        assert messages[cut].role != "tool", "the kept tail starts with an orphaned tool result"


def test_a_small_window_still_elides_something():
    """Regression: the trigger fired but the cut refused, so compaction silently did nothing.

    With keep_recent at 20k and a window smaller than that, walking back for the target never spent
    it, the cut stayed at 0, and compaction became a permanent no-op — the guard never fired.
    """
    messages = conversation()
    assert cut_index(messages, tail_budget_tokens=200) > 0
    # And with no budget at all (the pure keep-recent reading) it is allowed to keep everything.
    assert cut_index(messages) == 0


def test_the_summary_replaces_exactly_what_was_elided():
    messages = conversation()
    cut = cut_index(messages, tail_budget_tokens=500)
    stub = Completion(text="Goal: do the thing")
    compaction = summarise(stub, messages, cut)
    after = apply_compaction(messages, compaction)
    assert after[0].role == "user", "the summary is given to the model, not attributed to it"
    assert "Goal: do the thing" in after[0].content[0].text  # type: ignore[union-attr]
    assert after[1:] == messages[cut:]
    assert compaction.tokens_after < compaction.tokens_before


def test_the_summary_is_labelled_as_a_summary():
    compaction = summarise(Completion(text="Goal: x"), conversation(), 1)
    text = apply_compaction(conversation(), compaction)[0].content[0].text  # type: ignore[union-attr]
    assert "context summary" in text
    assert str(compaction.first_kept_index) in text, "the reader must know what was kept"


def test_a_mechanical_placeholder_says_so():
    """It must not be mistaken for a summary: an agent that trusts it trusts gaps it should not."""
    compaction = summarise(Completion(text="   "), conversation(), 3)
    assert compaction.fallback is True
    assert "mechanical" in compaction.summary.lower()
    assert "not a model summary" in compaction.summary


def test_the_previous_summary_is_passed_forward():
    """Iterative summarisation: compacting twice must not lose the first summary's content."""
    prompt = summary_prompt("Goal: the earlier goal", "the conversation")
    assert "the earlier goal" in prompt
    assert "the conversation" in prompt
    # Nothing to carry forward must not leave an empty section behind.
    assert "earlier summary" not in summary_prompt("", "the conversation")


def test_large_tool_output_is_not_dropped_from_the_summariser_input():
    """The elided prefix is exactly where the expensive detail lives; it has to be visible."""
    rendered = render_for_summary(conversation())
    assert "A" * 4000 in rendered
    assert "[tool call] read" in rendered


# --------------------------------------------------------------------------- the loop


class CompactingSuite:
    """A suite with a small window that answers the summariser call and scripts the rest."""

    def __init__(
        self,
        completions: list[Completion],
        *,
        context_window: int = 1_200,
        summary: str = "Goal: restore the function",
    ) -> None:
        self._completions = list(completions)
        self.context_window = context_window
        self.summary = summary
        #: Every request the loop made, so a test can assert what the model actually saw.
        self.calls: list[list[Message]] = []
        self.summary_calls: list[list[Message]] = []

    def resolve(self, ref: str) -> ModelInfo:
        return ModelInfo(
            id="fake",
            provider="fake",
            api="anthropic-messages",
            base_url="http://localhost",
            name="Fake",
            context_window=self.context_window,
            cost=Cost(input=1.0, output=1.0),
        )

    async def complete(self, model, messages, tools=None, **kw) -> Completion:
        if tools == []:
            # The summariser is the only caller that passes no tools.
            self.summary_calls.append(list(messages))
            return Completion(text=self.summary, usage=Usage(input_tokens=20, output_tokens=10))
        self.calls.append(list(messages))
        if not self._completions:
            raise AssertionError("loop asked for more turns than the script provides")
        completion = self._completions.pop(0)
        completion.cost_usd = model.cost.of(completion.usage)
        return completion


def usage() -> Usage:
    return Usage(input_tokens=100, output_tokens=50)


def big_read(call_id: str = "c1") -> Completion:
    """A turn that adds a large tool result, which is how a real run grows."""
    return Completion(
        tool_calls=[call(call_id, path="big.txt")], stop_reason="tool_use", usage=usage()
    )


#: A prompt large enough that replacing it saves more than the summary costs, and larger than
#: `KEEP_RECENT_TOKENS` so there is genuinely something to elide. `SUMMARY_MAX_TOKENS` is 1500, so a
#: smaller prefix would be declined by the guard rather than reach the summariser — the tests need to
#: exercise the path *past* the guard, not the guard itself.
BIG_QUESTION = "Goal: read the file and report.\n" + ("background detail\n" * 6_000)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "big.txt").write_text("line of text\n" * 2_000)
    return tmp_path


async def test_a_long_run_compacts_and_the_model_sees_the_summary(project: Path, tmp_path: Path):
    """The end-to-end assertion: the request after compaction contains the summary, not the prefix."""
    suite = CompactingSuite(
        [
            big_read("c1"),
            big_read("c2"),
            Completion(text="done", usage=usage()),
        ],
        context_window=20_000,
    )
    from aiden.session import SessionStore

    store = SessionStore.create(cwd=project, model="fake")
    result = await run_loop(BIG_QUESTION, suite=suite, model="fake", cwd=project, session=store)

    assert result.error == "", f"the run failed instead of compacting: {result.error}"
    assert suite.summary_calls, "the summariser was never called"

    # The assertion that matters: a later request carried the summary, so the prefix was genuinely
    # replaced rather than merely counted. Every wiring bug in this repository passed a helper test
    # and failed this one.
    def texts(messages: list[Message]) -> str:
        return "\n".join(
            part.text
            for message in messages
            for part in message.content
            if isinstance(part, TextPart)
        )

    assert "Goal: restore the function" in texts(suite.calls[-1])
    assert result.answer == "done"


async def test_a_compaction_is_recorded_in_the_transcript(project: Path, tmp_path: Path):
    """The transcript is the audit trail: a reader must be able to see why the agent forgot."""
    from aiden.session import SessionStore

    suite = CompactingSuite(
        [big_read("c1"), big_read("c2"), Completion(text="done", usage=usage())],
        context_window=20_000,
    )
    store = SessionStore.create(cwd=project, model="fake")
    await run_loop(BIG_QUESTION, suite=suite, model="fake", cwd=project, session=store)

    entries = read_entries(store.path)
    compactions = [e for e in entries if e.type == ENTRY_COMPACTION]
    assert compactions, "the compaction was not logged"
    payload = compactions[0].payload
    assert payload["summary"].startswith("Goal:")
    assert payload["tokens_before"] > payload["tokens_after"], "the saving must be visible"
    assert payload["retained_tail"] is False
    assert isinstance(payload["first_kept_entry_id"], int)


async def test_the_summariser_spend_is_in_the_run_total(project: Path, tmp_path: Path):
    """A summary is real money; hiding it would make the eval's cost column wrong."""
    from aiden.session import SessionStore

    suite = CompactingSuite(
        [big_read("c1"), big_read("c2"), Completion(text="done", usage=usage())],
        context_window=20_000,
    )
    store = SessionStore.create(cwd=project, model="fake")
    result = await run_loop(BIG_QUESTION, suite=suite, model="fake", cwd=project, session=store)
    assert result.cost_usd > 0
    assert result.usage.input_tokens > 100, "the summariser's tokens are missing from the total"


async def test_an_overflow_is_recovered_by_compacting(project: Path, tmp_path: Path):
    """A provider overflow is the one error the loop can fix itself: summarise, then resend."""
    from aiden.session import SessionStore

    overflow = Completion(
        stop_reason="error",
        error="prompt is too long: 210000 tokens > 200000 maximum",
        error_kind="overflow",
        usage=usage(),
    )
    suite = CompactingSuite(
        [big_read("c1"), big_read("c2"), overflow, Completion(text="recovered", usage=usage())],
        context_window=1_000_000,  # large, so only the overflow path can trigger compaction
    )
    store = SessionStore.create(cwd=project, model="fake")
    result = await run_loop(BIG_QUESTION, suite=suite, model="fake", cwd=project, session=store)

    assert suite.summary_calls, "the overflow was not met with a compaction"
    assert result.stop_reason != "error", "the run should have recovered"
    assert result.answer == "recovered"


async def test_a_non_overflow_error_still_ends_the_run(project: Path, tmp_path: Path):
    """Only overflow is recoverable. Compacting on an auth failure would hide a real problem."""
    from aiden.session import SessionStore

    suite = CompactingSuite(
        [
            big_read("c1"),
            big_read("c2"),
            Completion(
                stop_reason="error", error="invalid api key", error_kind="auth", usage=usage()
            ),
        ],
        context_window=1_000_000,
    )
    store = SessionStore.create(cwd=project, model="fake")
    result = await run_loop(BIG_QUESTION, suite=suite, model="fake", cwd=project, session=store)

    assert result.stop_reason == "error"
    assert "invalid api key" in result.error
    assert not suite.summary_calls, "compaction must not be tried for an unrecoverable error"


async def test_compaction_stops_at_the_cap(project: Path, tmp_path: Path):
    """Summarising summaries forever is worse than stopping: the cap is what bounds the loss."""
    from aiden.session import SessionStore

    # Every assistant turn adds another big tool result, so the context keeps refilling.
    completions = [big_read(f"c{i}") for i in range(1, 12)] + [
        Completion(text="end", usage=usage())
    ]
    suite = CompactingSuite(completions, context_window=900)
    store = SessionStore.create(cwd=project, model="fake")
    await run_loop(
        BIG_QUESTION, suite=suite, model="fake", cwd=project, session=store, max_turns=14
    )

    entries = read_entries(store.path)
    logged = [e for e in entries if e.type == ENTRY_COMPACTION]
    assert len(logged) <= config.MAX_COMPACTIONS


async def test_a_failing_summariser_does_not_lose_the_context(project: Path, tmp_path: Path):
    """Declining to compact is the honest failure: the alternative is silent truncation."""

    class BrokenSummariser(CompactingSuite):
        async def complete(self, model, messages, tools=None, **kw) -> Completion:
            if tools == []:
                raise RuntimeError("summariser unavailable")
            return await super().complete(model, messages, tools, **kw)

    from aiden.session import SessionStore

    suite = BrokenSummariser(
        [big_read("c1"), big_read("c2"), Completion(text="done", usage=usage())],
        context_window=20_000,
    )
    store = SessionStore.create(cwd=project, model="fake")
    result = await run_loop(BIG_QUESTION, suite=suite, model="fake", cwd=project, session=store)

    assert any("compaction failed" in note for note in result.diagnostics), result.diagnostics
    # The run continues with its original context rather than with a hole in it.
    assert result.answer == "done"


def test_the_trigger_and_the_cut_agree():
    """The guard must not be able to fire while the cut refuses, which is how it became a no-op."""
    messages = conversation()
    window = 1_000
    assert needs_compaction(messages * 40, "system", window) is True
    assert cut_index(messages * 40, tail_budget_tokens=window - config.RESERVE_TOKENS) > 0


def test_mechanical_note_names_what_is_unknown():
    note = mechanical_note(conversation()[:3], conversation()[3:])
    assert note.count("unknown") >= 3
    assert "the original question" in note


def test_replay_shows_the_compaction_and_its_summary(tmp_path: Path):
    """A compaction must be visible on replay, not only in the live run.

    It is the one event that makes the model's context diverge from the transcript. Replay without it
    shows messages the model never saw and gives no reason the agent forgot something.
    """
    from aiden.tui.replay import events_from_session

    store = SessionStore.create(cwd=tmp_path, model="fake")
    store.append(
        ENTRY_COMPACTION,
        {
            "summary": "Goal: restore the function",
            "first_kept_entry_id": 3,
            "retained_tail": False,
            "tokens_before": 30_000,
            "tokens_after": 8_000,
            "reason": "overflow",
            "fallback": False,
        },
    )
    messages = [
        event.message
        for event in events_from_session(store.path)
        if type(event).__name__ == "Diagnostic"
    ]
    assert any("30000 -> 8000" in text for text in messages), messages
    assert any("Goal: restore the function" in text for text in messages), messages
