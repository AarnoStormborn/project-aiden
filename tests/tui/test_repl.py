"""The interactive session: command parsing, state, and the loop.

The loop is driven with an injected input source and a fake suite, so the whole session is
testable without a terminal or a provider.
"""

from __future__ import annotations

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

        def emit(self, event) -> None:
            self.events.append(event)

        def finish(self) -> None:
            return None

        def abort(self) -> None:
            return None

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
