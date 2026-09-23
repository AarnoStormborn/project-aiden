"""Interactive session: a prompt, a run, repeat.

`aiden ask` answers one question and exits. This makes the TUI a *session*: read a question, stream
the run inline, then read the next one, with the transcript accumulating in scrollback.

Deliberate scope:

- **Input happens between runs, not during one.** While a run streams, the prompt is not reading,
  so the live region owns the screen and there is no contention over the cursor. Steering a run
  mid-flight is the next feature and needs the editor and the driver coordinated through
  ``patch_stdout``; pretending to support it now would mean an input path that silently drops
  keystrokes.
- **Commands are local.** A leading ``/`` is handled here, never sent to the model, so a typo in a
  command cannot become a billed request.

Command parsing is pure and separately tested; the input source is injectable, so the loop can be
driven without a terminal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..checkpoint import CheckpointStore
from ..loop import LoopResult, run_loop
from ..providers import ProviderSuite
from ..session import SessionStore, list_sessions
from .approval_ui import PromptApprover
from .driver import TUIDriver
from .keys import Keymap

PROMPT = "› "
CONTINUATION = "  "

#: Every local command, with the line shown by /help. The *only* place a command is declared:
#: the help text and ``Command.known`` are both derived from it, so a handler cannot be wired up
#: without also being registered — which is exactly how `/undo` shipped as unreachable dead code.
COMMANDS: dict[str, str] = {
    "help": "this text",
    "model": "show or set the model (provider/model[:thinking])",
    "turns": "set the turn ceiling for the next question",
    "cost": "spend so far this session",
    "transcript": "reprint this session's questions and answers",
    "sessions": "list recorded sessions for this project",
    "keys": "show the key bindings",
    "clear": "forget the in-session transcript (does not delete the log)",
    "undo": "revert the files changed by the last turn",
    "quit": "leave (also Ctrl-D)",
}


def _help_text() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = ["aiden — interactive session", ""]
    for name, description in COMMANDS.items():
        lines.append(f"  /{name.ljust(width)}   {description}")
    lines.append("")
    lines.append("Anything else is sent to the model. Ctrl-C interrupts a running turn.")
    return "\n".join(lines)


HELP_TEXT = _help_text()


@dataclass(slots=True)
class Command:
    """A parsed local command."""

    name: str
    argument: str = ""

    @property
    def known(self) -> bool:
        return self.name in COMMANDS


def parse_command(line: str) -> Command | None:
    """Parse a local command, or ``None`` when the line is a question.

    Only a leading ``/`` counts. A question containing a slash (a path, a URL) is not a command,
    and an empty line is not one either.
    """
    stripped = line.strip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:].strip()
    if not body:
        return None
    name, _, argument = body.partition(" ")
    name = name.lower()
    if name == "exit":
        name = "quit"
    return Command(name=name, argument=argument.strip())


@dataclass
class SessionState:
    """Everything that persists across questions in one interactive session."""

    model: str = ""
    max_turns: int = 0
    spent_usd: float = 0.0
    turns_used: int = 0
    tool_calls: int = 0
    history: list[tuple[str, str]] = field(default_factory=list)


class AidenSession:
    """The interactive loop. Input and output are injectable for testing."""

    def __init__(
        self,
        *,
        suite: ProviderSuite | None = None,
        cwd: Path | None = None,
        model: str | None = None,
        max_turns: int | None = None,
        max_cost_usd: float | None = None,
        driver: TUIDriver | None = None,
        read_input: Callable[[str], str | None] | None = None,
        write: Callable[[str], None] | None = None,
        keymap: Keymap | None = None,
        resume: Path | None = None,
    ) -> None:
        self.suite = suite or ProviderSuite.load()
        self.cwd = (cwd or Path.cwd()).resolve()
        self.model_ref = model or config.DEFAULT_MODEL
        self.max_cost_usd = max_cost_usd if max_cost_usd is not None else config.MAX_RUN_COST_USD
        self.keymap = keymap or Keymap.load(_keymap_path())
        self.driver = driver or TUIDriver()
        self._read = read_input
        self._write = write or print
        self.state = SessionState(model=self.model_ref, max_turns=max_turns or config.MAX_TURNS)
        self.store = SessionStore.create(cwd=self.cwd, model=self.model_ref)
        self.resume_path = resume
        self._banner_shown = False
        self.checkpoints = CheckpointStore(self.store.session_id, cwd=self.cwd)
        self.approver = PromptApprover(driver=self.driver)
        if resume is not None:
            # Resuming keeps writing to the same log, so the transcript stays one story rather
            # than forking into a second file the user has to reconcile.
            self._load_resume(resume)

    def _load_resume(self, path: Path) -> None:
        """Reopen a recorded session: replay it visibly, then continue the same log."""
        from .replay import events_from_session, questions_from_session

        for event in events_from_session(path):
            self.driver.emit(event)
        for question in questions_from_session(path):
            previous = self._answers.get(question, "")
            self.state.history.append((question, previous))
        self.store = SessionStore.open(path)

    @property
    def _answers(self) -> dict[str, str]:
        """Answers recorded in the resumed log, keyed by question, for `/transcript`."""
        answers: dict[str, str] = {}
        if self.resume_path is None:
            return answers
        from ..session import ENTRY_ASSISTANT, read_entries

        last_question = ""
        for entry in read_entries(self.resume_path):
            if entry.type == "user_message":
                last_question = entry.payload.get("text", "")
            elif entry.type == ENTRY_ASSISTANT and last_question:
                text = entry.payload.get("text", "")
                if text:
                    answers[last_question] = text
        return answers

    # ------------------------------------------------------------------ io

    async def _read_line(self, prompt: str = PROMPT) -> str | None:
        if self._read is not None:
            return self._read(prompt)
        return await _prompt_toolkit(prompt, self.state.history)

    def _say(self, text: str) -> None:
        self._write(text)

    # ------------------------------------------------------------------ loop

    async def run(self) -> int:
        """Read questions until EOF or /quit. Returns a process exit code.

        A SIGINT during a turn unwinds out of ``asyncio.run`` rather than out of this coroutine —
        the exception is raised in the event loop's selector — so this loop cannot catch it and
        does not try. The *session object* survives, and the CLI re-enters :meth:`run` after
        calling :meth:`interrupted`. An earlier version installed an asyncio SIGINT handler to
        cancel just the turn; it left the process unresponsive to all input, which is worse than
        the bug it fixed.
        """
        if not self._banner_shown:
            self._say(f"aiden · {self.state.model} · {self.cwd}")
            self._say("type /help for commands, Ctrl-C to interrupt a turn, Ctrl-D to leave\n")
            self._banner_shown = True

        while True:
            try:
                line = await self._read_line()
            except EOFError:
                return self._leave()
            except KeyboardInterrupt:
                # Ctrl-C *at the prompt* discards the line and stays in the session, which is what
                # "/help" promises. Only Ctrl-D and /quit leave. An earlier version exited the
                # whole session on either.
                self._say("")
                continue
            if line is None:
                return self._leave()
            if not line.strip():
                continue

            command = parse_command(line)
            if command is not None:
                if command.name in ("quit",):
                    return self._leave()
                self._handle_command(command)
                continue

            await self._ask(line)

    async def _ask(self, question: str) -> LoopResult:
        result = await run_loop(
            question,
            suite=self.suite,
            model=self.state.model,
            cwd=self.cwd,
            sink=self.driver,
            session=self.store,
            max_turns=self.state.max_turns,
            max_cost_usd=self.max_cost_usd,
            max_tokens=config.DEFAULT_MAX_TOKENS,
            thinking_level=config.DEFAULT_THINKING_LEVEL,
            approver=self.approver,
            checkpoints=self.checkpoints,
        )
        self.state.spent_usd += result.cost_usd
        self.state.turns_used += result.turns
        self.state.tool_calls += result.tool_calls
        self.state.history.append((question, result.answer))
        return result

    # ------------------------------------------------------------------ commands

    def handlers(self) -> dict[str, Callable[[Command], None]]:
        """Handler per command. Its keys must equal ``COMMANDS`` — see the guard test."""
        return {
            "help": lambda _command: self._say(HELP_TEXT),
            "model": lambda command: self._command_model(command.argument),
            "turns": lambda command: self._command_turns(command.argument),
            "cost": lambda _command: self._command_cost(),
            "transcript": lambda _command: self._command_transcript(),
            "sessions": lambda _command: self._command_sessions(),
            "keys": lambda _command: self._command_keys(),
            "clear": lambda _command: self._command_clear(),
            "undo": lambda _command: self._command_undo(),
            "quit": lambda _command: None,  # handled by the caller, which returns from run()
        }

    def _handle_command(self, command: Command) -> None:
        handler = self.handlers().get(command.name)
        if handler is None:
            self._say(f"unknown command /{command.name} — try /help")
            self._say(HELP_TEXT)
            return
        handler(command)

    def _command_cost(self) -> None:
        totals = self.session_totals()
        # Reported from the session log, not from in-memory counters. An interrupted run never
        # returns from `_ask`, so counters drift low — the log recorded the real cost.
        self._say(
            f"spent ${totals['cost_usd']:.4f} · {totals['turns']} turns · "
            f"{totals['tool_calls']} tool calls"
        )
        if totals["interrupted"]:
            self._say(f"  ({totals['interrupted']} interrupted run(s) included)")

    def _command_keys(self) -> None:
        for chord, action in self.keymap.help_rows():
            self._say(f"  {chord:18} {action}")

    def _command_clear(self) -> None:
        self.state.history.clear()
        self._say("in-session transcript cleared (the session log is untouched)")

    def _command_model(self, argument: str) -> None:
        if not argument:
            self._say(f"model: {self.state.model}")
            return
        try:
            resolved = self.suite.registry.resolve(argument)
        except KeyError as exc:
            self._say(f"error: {exc}")
            # A bare provider name is the common mistake ("/model anthropic" instead of
            # "/model anthropic/claude-sonnet-5"), and the catalogue can answer it directly.
            if "/" not in argument:
                example = self._example_model(argument)
                if example:
                    self._say(f"did you mean /model {example} ? (a model ref is provider/model)")
            return
        self.state.model = argument
        self._say(f"model: {resolved.model.ref} · thinking {resolved.thinking_level or 'default'}")

    def _example_model(self, provider: str) -> str | None:
        """First usable model for a provider, for the 'did you mean' hint."""
        try:
            models = self.suite.registry.by_provider(provider)
        except KeyError:
            return None
        return models[0].ref if models else None

    def _command_turns(self, argument: str) -> None:
        if not argument:
            self._say(f"turn ceiling: {self.state.max_turns}")
            return
        try:
            value = int(argument)
        except ValueError:
            self._say(f"error: '{argument}' is not a number")
            return
        if not 1 <= value <= 100:
            self._say("error: the turn ceiling must be between 1 and 100")
            return
        self.state.max_turns = value
        self._say(f"turn ceiling: {value}")

    def session_totals(self) -> dict[str, float]:
        """Spend, turns and tool calls for this session, read from the log."""
        from ..session import ENTRY_RUN_END, read_entries

        cost = 0.0
        turns = 0
        calls = 0
        interrupted = 0
        try:
            entries = list(read_entries(self.store.path))
        except OSError:
            entries = []
        for entry in entries:
            if entry.type != ENTRY_RUN_END:
                continue
            payload = entry.payload
            cost += float(payload.get("cost_usd", 0.0) or 0.0)
            turns += int(payload.get("turns", 0) or 0)
            calls += int(payload.get("tool_calls", 0) or 0)
            if payload.get("stop_reason") == "aborted":
                interrupted += 1
        return {
            "cost_usd": cost,
            "turns": turns,
            "tool_calls": calls,
            "interrupted": interrupted,
        }

    def _command_undo(self) -> None:
        """Revert the files changed by the most recent turn."""
        checkpoint = self.checkpoints.latest()
        if checkpoint is None:
            self._say("nothing to undo: no files have been changed in this session")
            return
        changed = self.checkpoints.restore(checkpoint)
        if not changed:
            self._say(f"turn {checkpoint.turn} changed no files that still exist")
            return
        self._say(f"reverted turn {checkpoint.turn}:")
        for path in changed:
            self._say(f"  {path}")
        # The revert changes files behind the model's back, so its read hashes are now wrong.
        self.driver.recover()

    def _command_transcript(self) -> None:
        if not self.state.history:
            self._say("nothing asked yet")
            return
        for index, (question, answer) in enumerate(self.state.history, 1):
            self._say(f"{index}. {question}")
            for line in (answer or "(no answer)").splitlines():
                self._say(f"   {line}")

    def _command_sessions(self) -> None:
        rows = list_sessions(self.cwd)
        if not rows:
            self._say("no sessions recorded for this project")
            return
        for row in rows[:10]:
            self._say(
                f"  {row['session_id']}  {row['entries']:>4} entries  {row['started_at'][:19]}"
            )

    def interrupted(self) -> None:
        """Recover after SIGINT tore down the event loop, so the session can continue.

        The turn was cancelled part-way, so the transcript is marked aborted (partial answer kept)
        and the live-region bookkeeping is dropped rather than patched: after an interrupt the
        cursor may sit mid-frame and the writer's belief about the screen is no longer true.
        """
        self.driver.abort()
        self._say("interrupted")
        self.driver.recover()

    def _leave(self) -> int:
        self.driver.finish()
        self.store.close()
        self._say(f"\nsession {self.store.path}")
        return 0


def _keymap_path() -> Path | None:
    import os

    explicit = os.environ.get("AIDEN_KEYS_FILE")
    if explicit:
        return Path(explicit)
    candidate = config.AIDEN_HOME / "keys.toml"
    return candidate if candidate.is_file() else None


_SESSION: Any = None
_HISTORY_SYNCED = 0


def _prompt_session() -> Any:
    """Build the editor once. Enter submits; Alt-Enter or Ctrl-J inserts a newline."""
    global _SESSION
    if _SESSION is None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings

        bindings = KeyBindings()

        @bindings.add("escape", "enter")
        def _newline(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        _SESSION = PromptSession(
            history=InMemoryHistory(),
            key_bindings=bindings,
            multiline=False,
            # Alt-Enter is the newline chord; keep Enter as submit.
            enable_open_in_editor=False,
        )
    return _SESSION


async def _prompt_toolkit(prompt: str, history: list[tuple[str, str]]) -> str | None:
    """Read one line inside the running loop.

    ``PromptSession.prompt()`` is synchronous and calls ``asyncio.run`` internally, which raises
    "cannot be called from a running event loop" when invoked from our session coroutine — so this
    must be the async variant. The failure only appeared when the TUI was actually launched, since
    the tests inject their own input source.
    """
    global _HISTORY_SYNCED
    session = _prompt_session()

    for question, _answer in history[_HISTORY_SYNCED:]:
        session.history.append_string(question)
    _HISTORY_SYNCED = len(history)

    return await session.prompt_async(prompt)


def iter_inputs(lines: Iterator[str]) -> Callable[[str], str | None]:
    """Wrap an iterable of lines as a ``read_input`` callable, for tests and scripts."""
    iterator = iter(lines)

    def read(_prompt: str = PROMPT) -> str | None:
        try:
            return next(iterator)
        except StopIteration:
            return None

    return read
