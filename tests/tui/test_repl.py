"""The interactive session: command parsing, state, and the loop.

The loop is driven with an injected input source and a fake suite, so the whole session is
testable without a terminal or a provider.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from aiden.providers.types import Completion, Cost, ModelInfo, Usage
from aiden.tui.repl import AidenSession, Command, iter_inputs, parse_command

# --------------------------------------------------------------------------- parsing


def test_a_leading_slash_is_a_command():
    assert parse_command("/help") == Command("help")
    assert parse_command("/model opencode-go/x") == Command("model", "opencode-go/x")
    assert parse_command("  /turns 5  ") == Command("turns", "5")


def test_exit_is_an_alias_for_quit():
    assert parse_command("/exit") == Command("quit")


def test_a_question_is_not_a_command():
    assert parse_command("what does config.py define?") is None
    assert parse_command("") is None
    assert parse_command("   ") is None
    assert parse_command("/") is None


def test_slashes_inside_a_question_are_untouched():
    """A path or URL must not be mistaken for a command."""
    assert parse_command("read docs/plan/providers.md") is None
    assert parse_command("see https://example.com/a/b") is None


def test_command_names_are_case_insensitive():
    assert parse_command("/HELP") == Command("help")


# --------------------------------------------------------------------------- loop


def _resolve(ref: str) -> ModelInfo:
    """Mirror the real registry's contract: a ref must be ``provider/model``.

    A fake that resolves a bare provider name hides the very mistake the hint exists for.
    """
    if "not-a-real" in ref or "/" not in ref:
        raise KeyError(f"no model matches {ref!r}")
    return _fake_model(ref)


def _fake_model(ref: str) -> ModelInfo:
    return ModelInfo(
        id=ref.partition("/")[2] or "fake",
        provider=ref.partition("/")[0] or "fake",
        api="anthropic-messages",
        base_url="http://localhost",
        name="Fake",
        cost=Cost(input=1.0, output=1.0),
    )


class FakeSuite:
    """Minimal provider suite: answers every question with a fixed response.

    Faithful to the two entry points ``run_loop`` uses — ``resolve`` (by ref) and ``complete`` —
    because a fake that omits one tests nothing but the fake.
    """

    def __init__(self, answer: str = "the answer") -> None:
        self.answer = answer
        self.asked: list[str] = []
        self.registry = self._Registry()

    class _Registry:
        def resolve(self, ref: str):
            from aiden.providers.registry import Resolved

            return Resolved(model=_resolve(ref), thinking_level=None)

        def by_provider(self, provider: str) -> list[ModelInfo]:
            if "not-a-real" in provider:
                raise KeyError(provider)
            return [_fake_model(f"{provider}/example-model")]

    def resolve(self, ref: str) -> ModelInfo:
        return _resolve(ref)

    async def complete(self, model, messages, tools=None, **kw) -> Completion:
        question = messages[0].content[0].text  # type: ignore[union-attr]
        self.asked.append(question)
        return Completion(
            text=self.answer,
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=5),
            cost_usd=0.0005,
        )


@pytest.fixture
def session(tmp_path: Path, monkeypatch) -> tuple[AidenSession, list[str]]:
    from aiden import config

    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config, "SPILL_DIR", tmp_path / "spill")

    lines: list[str] = []
    out: list[str] = []

    class CapturingDriver:
        """Records events so the test can assert on what a real driver would render."""

        def __init__(self) -> None:
            self.events: list = []
            self.aborted = False

        def emit(self, event) -> None:
            self.events.append(event)

        def finish(self) -> None:
            return None

        def abort(self) -> None:
            self.aborted = True

        def recover(self) -> None:
            self.recovered = True

        @property
        def text(self) -> str:
            from aiden.events import TextDelta

            return "".join(e.text for e in self.events if isinstance(e, TextDelta))

    repl = AidenSession(
        suite=FakeSuite(),  # type: ignore[arg-type]
        cwd=tmp_path,
        model="fake/model",
        driver=CapturingDriver(),  # type: ignore[arg-type]
        read_input=iter_inputs(iter(lines)),
        write=out.append,
    )
    return repl, lines, out  # type: ignore[return-value]


async def test_quit_leaves_the_loop(session):
    repl, lines, _out = session
    lines.extend(["/quit"])
    assert await repl.run() == 0


async def test_eof_leaves_the_loop(session):
    repl, lines, _out = session
    lines.clear()
    assert await repl.run() == 0


async def test_a_question_is_answered_and_recorded(session):
    repl, lines, out = session
    lines.extend(["what is 2+2?", "/quit"])
    await repl.run()

    assert repl.state.history == [("what is 2+2?", "the answer")]
    # The answer is rendered by the driver, not by the REPL's own writer.
    assert repl.driver.text == "the answer"  # type: ignore[attr-defined]
    assert out, "the session should print its banner and prompt guidance"


async def test_cost_accumulates_across_questions(session):
    repl, lines, _out = session
    lines.extend(["one", "two", "/quit"])
    await repl.run()
    assert repl.state.spent_usd == pytest.approx(0.001)
    assert len(repl.state.history) == 2


async def test_blank_lines_are_ignored(session):
    repl, lines, _out = session
    lines.extend(["", "   ", "real question", "/quit"])
    await repl.run()
    assert len(repl.state.history) == 1


async def test_unknown_command_does_not_reach_the_model(session):
    """A typo must not become a billed request."""
    repl, lines, out = session
    lines.extend(["/nonsense", "/quit"])
    await repl.run()

    assert repl.state.history == []
    assert repl.suite.asked == []  # type: ignore[union-attr]
    assert any("unknown command" in line for line in out)


async def test_help_lists_the_commands(session):
    repl, lines, out = session
    lines.extend(["/help", "/quit"])
    await repl.run()
    body = "\n".join(out)
    for command in ("/model", "/transcript", "/sessions", "/quit"):
        assert command in body


async def test_model_command_reports_and_rejects(session):
    repl, lines, out = session
    lines.extend(["/model", "/model not-a-real-provider/x", "/quit"])
    await repl.run()
    body = "\n".join(out)
    assert "model: fake/model" in body
    assert "error:" in body
    # A failed switch must not change the active model.
    assert repl.state.model == "fake/model"


async def test_turns_command_sets_the_ceiling_and_validates(session):
    repl, lines, out = session
    lines.extend(["/turns 5", "/turns nonsense", "/turns 999", "/quit"])
    await repl.run()
    assert repl.state.max_turns == 5
    body = "\n".join(out)
    assert "is not a number" in body
    assert "between 1 and 100" in body


async def test_cost_command_reports_the_session_total(session):
    repl, lines, out = session
    lines.extend(["a question", "/cost", "/quit"])
    await repl.run()
    assert any("spent $" in line for line in out)


async def test_transcript_command_reprints_questions(session):
    repl, lines, out = session
    lines.extend(["first question", "/transcript", "/quit"])
    await repl.run()
    assert any("1. first question" in line for line in out)


async def test_clear_forgets_history_but_not_the_log(session):
    repl, lines, _out = session
    lines.extend(["a question", "/clear", "/transcript", "/quit"])
    await repl.run()
    assert repl.state.history == []
    # The session file is still the record of what happened.
    assert repl.store.path.exists()
    assert "a question" in repl.store.path.read_text()


async def test_sessions_command_lists_the_current_session(session):
    repl, lines, out = session
    lines.extend(["/sessions", "/quit"])
    await repl.run()
    assert any(repl.store.session_id in line for line in out)


async def test_keys_command_lists_bindings(session):
    repl, lines, out = session
    lines.extend(["/keys", "/quit"])
    await repl.run()
    body = "\n".join(out)
    assert "thinking.toggle" in body
    assert "run.interrupt" in body


async def test_the_session_log_records_every_question(session):
    repl, lines, _out = session
    lines.extend(["first", "second", "/quit"])
    await repl.run()
    log = repl.store.path.read_text()
    assert "first" in log and "second" in log


async def test_model_command_hints_a_full_ref_for_a_bare_provider(session):
    """`/model anthropic` is the common mistake; the hint should name a real model ref."""
    repl, lines, out = session
    lines.extend(["/model anthropic", "/quit"])
    await repl.run()

    body = "\n".join(out)
    assert "did you mean /model anthropic/example-model" in body
    assert repl.state.model == "fake/model", "a failed switch must not change the model"


# --------------------------------------------------------------------------- interruption


async def test_ctrl_c_at_the_prompt_stays_in_the_session(session):
    """`/help` promises Ctrl-C interrupts a turn; at the prompt it must not end the session."""
    repl, _lines, out = session

    calls = {"n": 0}

    def flaky(_prompt: str = "") -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        if calls["n"] == 2:
            return "a real question"
        return None  # EOF

    repl._read = flaky
    code = await repl.run()

    assert code == 0
    assert len(repl.state.history) == 1, "the session continued after Ctrl-C"
    assert any("a real question" in q for q, _a in repl.state.history)
    # The Ctrl-C itself is acknowledged (a blank line) rather than leaving a half-drawn prompt.
    assert out, "the session should have written its banner before the interrupt"


async def test_interrupted_recovers_the_session_for_another_run(session, monkeypatch):
    """SIGINT tears down the event loop, not the session: the object survives and is reusable.

    An earlier attempt installed an asyncio SIGINT handler to cancel just the turn. It left the
    process unresponsive to all input, which is worse than the bug it fixed, so the CLI re-enters
    `run()` instead.
    """
    repl, lines, out = session

    async def interrupted_ask(_question: str):
        raise KeyboardInterrupt

    monkeypatch.setattr(repl, "_ask", interrupted_ask)
    lines.extend(["a question"])

    with pytest.raises(KeyboardInterrupt):
        await repl.run()

    # What the CLI does next.
    repl.interrupted()
    assert repl.driver.aborted, "the driver must be told to commit partials"
    assert "interrupted" in "\n".join(out)
    assert repl.driver.recovered, "the driver must be made reusable for the next run"
    # And the real driver does exactly that.
    from aiden.events import RunStarted, TextDelta
    from aiden.tui.driver import TUIDriver
    from aiden.tui.theme import Theme

    real = TUIDriver(out=io.StringIO(), theme=Theme(colour=False), width=80)
    real.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    real.emit(TextDelta(text="partial"))
    real.abort()
    real.recover()
    assert real.region.drawn_lines == []
    assert real.stream.text == ""

    # And a subsequent run works, with state intact.
    monkeypatch.setattr(repl, "_ask", lambda q: _answer(repl, q))
    lines.extend(["second question", "/quit"])
    assert await repl.run() == 0
    # Only completed Q&A pairs are recorded: the interrupted question produced no answer, and
    # pretending otherwise would put a half-answer into /transcript.
    assert repl.state.history == [("second question", "the answer")]


async def _answer(repl, question: str):
    from aiden.loop import LoopResult

    repl.state.history.append((question, "the answer"))
    return LoopResult(answer="the answer")


async def test_the_banner_is_shown_once_across_re_entries(session):
    """Re-entering the loop after an interrupt must not reprint the banner."""
    repl, lines, out = session
    lines.extend(["/quit"])
    await repl.run()
    lines.clear()
    await repl.run()
    assert sum("type /help for commands" in line for line in out) == 1


async def test_eof_leaves_the_session(session):
    repl, lines, _out = session
    lines.clear()  # immediate EOF (Ctrl-D)
    assert await repl.run() == 0


async def test_cost_is_read_from_the_log_not_from_counters(session):
    """An interrupted run never returns from `_ask`, so counters drift low; the log does not."""
    repl, lines, out = session
    lines.extend(["/cost", "/quit"])
    await repl.run()
    body = "\n".join(out)
    assert "spent $" in body
    # With no completed runs there is nothing to report, and it says so rather than inventing.
    assert "$0.0000" in body


# --------------------------------------------------------------------------- command registry


def test_every_handler_is_a_registered_command(session):
    """Regression: `/undo` shipped with a handler that could never run.

    The dispatch chain was added but the command was not registered, so `Command.known` was False,
    the handler was unreachable, and the command silently printed help instead. Helpers now come from
    one table and this test pins the two together.
    """
    from aiden.tui.repl import COMMANDS

    repl, _lines, _out = session
    handlers = set(repl.handlers())
    assert handlers == set(COMMANDS), (
        f"handlers without a command: {handlers - set(COMMANDS)}; "
        f"commands without a handler: {set(COMMANDS) - handlers}"
    )


def test_help_lists_every_registered_command(session):
    from aiden.tui.repl import COMMANDS, HELP_TEXT

    for name in COMMANDS:
        assert f"/{name}" in HELP_TEXT, f"/{name} is registered but not documented"


async def test_undo_with_nothing_to_undo_says_so(session):
    repl, lines, out = session
    lines.extend(["/undo", "/quit"])
    await repl.run()
    assert any("nothing to undo" in line for line in out)


async def test_undo_reverts_the_last_turn(session, tmp_path):
    """End to end through the REPL: a change, then /undo, file back to its pre-turn state."""
    repl, lines, out = session
    target = repl.cwd / "notes.md"

    checkpoint = repl.checkpoints.begin(1)
    repl.checkpoints.capture(checkpoint, target)
    target.write_text("changed by the agent\n")

    lines.extend(["/undo", "/quit"])
    await repl.run()

    assert not target.exists(), "a file created during the turn must be removed by /undo"
    assert any("reverted turn 1" in line for line in out)
