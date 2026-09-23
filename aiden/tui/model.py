"""The transcript model: typed cells, and a reducer from harness events.

Two decisions from research/06 §Information architecture:

- **The transcript is a list of typed cells, not a text stream.** Cells give collapse, expand,
  re-render at a new width, and cheap status colour; a flat string does none of those. The same
  cell list is reconstructible from the session ``.jsonl`` alone, which is what makes the
  sessions browser and the TUI the same program.
- **The UI consumes events and never state** (architecture §10), so this module is a pure
  reducer: ``Transcript.apply(event)`` in, ``cells`` out. No terminal, no I/O — which is why the
  golden tests can run it headless.

The model deliberately knows nothing about escape sequences or widths; that is ``render.py``.
The ``Cell -> Lines`` boundary is what keeps a Textual shell as an escape hatch later.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..events import (
    ApprovalRequested,
    ApprovalResolved,
    Diagnostic,
    Event,
    RunFinished,
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnStarted,
    UsageUpdated,
)
from ..providers.types import Usage

# Cell status values. Status is rendered as glyph + colour, never colour alone.
PENDING = "pending"
RUNNING = "running"
OK = "ok"
ERROR = "error"
ABORTED = "aborted"

#: Visible lines of tool output before collapsing to a summary (spec: default 12).
TOOL_VISIBLE_LINES = 12


@dataclass(slots=True)
class Cell:
    """Base cell. Subclasses carry the payload; ``status`` drives glyph and colour."""

    status: str = OK
    #: Monotonic id, used by the writer to key damage rects and by tests to be explicit.
    id: int = 0


@dataclass(slots=True)
class UserPrompt(Cell):
    text: str = ""
    status: str = OK


@dataclass(slots=True)
class AssistantText(Cell):
    text: str = ""
    #: Text already committed to scrollback. The remainder is the mutable tail.
    committed: str = ""
    status: str = RUNNING


@dataclass(slots=True)
class Thinking(Cell):
    text: str = ""
    collapsed: bool = True
    status: str = OK


@dataclass(slots=True)
class ToolCall(Cell):
    call_id: str = ""
    name: str = ""
    arguments: dict = field(default_factory=dict)
    output: str = ""
    duration_ms: int = 0
    truncated: bool = False
    skipped: bool = False
    expanded: bool = False
    status: str = RUNNING

    @property
    def visible_lines(self) -> int:
        return TOOL_VISIBLE_LINES

    def hidden_line_count(self) -> int:
        total = len(self.output.splitlines())
        return max(0, total - self.visible_lines)


@dataclass(slots=True)
class Approval(Cell):
    """A proposed change awaiting, or having received, a decision."""

    call_id: str = ""
    name: str = ""
    path: str = ""
    diff: str = ""
    sensitive: bool = False
    decided_by: str = ""
    note: str = ""
    status: str = PENDING


@dataclass(slots=True)
class Notice(Cell):
    text: str = ""
    level: str = "warning"
    status: str = OK


@dataclass(slots=True)
class TurnMarker(Cell):
    turn: int = 0
    status: str = OK


@dataclass(slots=True)
class RunSummary(Cell):
    stop_reason: str = ""
    turns: int = 0
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    session_path: str = ""
    error: str = ""
    status: str = OK


class Transcript:
    """Ordered cells plus the reducer that builds them from events."""

    def __init__(self) -> None:
        self.cells: list[Cell] = []
        self._next_id = 1
        self._open_assistant: AssistantText | None = None
        self._open_thinking: Thinking | None = None
        self._tools: dict[str, ToolCall] = {}

    # ------------------------------------------------------------------ reducer

    def apply(self, event: Event) -> None:
        if isinstance(event, RunStarted):
            self._add(UserPrompt(text=event.question))
            self._add(
                Notice(
                    text=f"{event.model} · {event.cwd}",
                    level="info",
                )
            )
        elif isinstance(event, TurnStarted):
            # A turn boundary ends the current text segment, so the next delta starts a fresh cell.
            # Without this, every turn's prose accumulated into one cell and rendered above the
            # marker of the turn that produced it.
            self._close_assistant()
            # Turn markers are only useful when a run actually has more than one turn; emitting
            # them unconditionally would add a line of chrome to every single-turn answer.
            if event.turn > 1:
                self._add(TurnMarker(turn=event.turn))
        elif isinstance(event, TextDelta):
            assistant = self._assistant()
            assistant.text += event.text
            assistant.status = RUNNING
        elif isinstance(event, ThinkingDelta):
            thinking = self._thinking()
            thinking.text += event.text
        elif isinstance(event, ToolCallStarted):
            self._close_assistant()
            started_call = ToolCall(
                call_id=event.call_id,
                name=event.name,
                arguments=dict(event.arguments),
                status=RUNNING,
            )
            self._tools[event.call_id] = started_call
            self._add(started_call)
        elif isinstance(event, ToolCallFinished):
            finished = self._tools.get(event.call_id)
            if finished is None:
                # A finish without a start happens if a recording begins mid-turn; synthesise
                # the cell so the result is not lost.
                finished = self._add(ToolCall(call_id=event.call_id, name=event.name))
                self._tools[event.call_id] = finished
            call: ToolCall = finished
            call.duration_ms = event.duration_ms
            call.truncated = event.truncated
            call.skipped = event.skipped
            call.status = ERROR if event.is_error else OK
            if event.output:
                call.output = event.output
        elif isinstance(event, ApprovalRequested):
            self._close_assistant()
            self._add(
                Approval(
                    call_id=event.call_id,
                    name=event.name,
                    path=event.path,
                    diff=event.diff,
                    sensitive=event.sensitive,
                    note=event.reason,
                )
            )
        elif isinstance(event, ApprovalResolved):
            for cell in self.cells:
                if isinstance(cell, Approval) and cell.call_id == event.call_id:
                    cell.status = OK if event.approved else ERROR
                    cell.decided_by = event.decided_by
                    if event.note:
                        cell.note = event.note
                    break
        elif isinstance(event, Diagnostic):
            self._add(Notice(text=event.message, level=event.level))
        elif isinstance(event, UsageUpdated):
            pass  # rolled into the run summary; not a transcript line of its own
        elif isinstance(event, RunFinished):
            self._close_assistant(final=True)
            self._open_thinking = None
            self._add(
                RunSummary(
                    stop_reason=event.stop_reason,
                    turns=event.turns,
                    tool_calls=event.tool_calls,
                    usage=event.usage,
                    cost_usd=event.cost_usd,
                    session_path=event.session_path,
                    error=event.error,
                    status=ERROR if event.error else OK,
                )
            )

    def finalize(self) -> None:
        """Close any streaming cells, e.g. on abort or normal completion."""
        self._close_assistant(final=True)

    # ------------------------------------------------------------------ queries

    @property
    def last(self) -> Cell | None:
        return self.cells[-1] if self.cells else None

    def of[T: Cell](self, kind: type[T]) -> list[T]:
        return [c for c in self.cells if isinstance(c, kind)]

    def tool_calls(self) -> list[ToolCall]:
        return self.of(ToolCall)

    def tail(self) -> AssistantText | None:
        """The streaming assistant cell, whose uncommitted text is the mutable region."""
        return self._open_assistant

    # ------------------------------------------------------------------ internals

    def _add[T: Cell](self, cell: T) -> T:
        cell.id = self._next_id
        self._next_id += 1
        self.cells.append(cell)
        return cell

    def _assistant(self) -> AssistantText:
        if self._open_assistant is None:
            self._open_assistant = self._add(AssistantText())
        return self._open_assistant

    def _thinking(self) -> Thinking:
        if self._open_thinking is None:
            self._open_thinking = self._add(Thinking())
        return self._open_thinking

    def _close_assistant(self, *, final: bool = False) -> None:
        """End the current text segment.

        This genuinely clears the open cell. An earlier version only flipped its status, so the
        next ``TextDelta`` — after a tool call, or in the next turn — appended to the *same* cell.
        The visible symptom was a transcript where a turn's answer appeared above that turn's
        marker, and where per-cell line accounting could not tell the segments apart.

        It deliberately does **not** touch ``committed``. That field means "text already written to
        scrollback", which is the *driver's* rendering state, not something the reducer can know.
        Setting it here made the driver believe the whole cell had been emitted, so finalized
        text silently never reached the transcript.
        """
        cell = self._open_assistant
        if cell is None:
            return
        cell.status = OK
        self._open_assistant = None
