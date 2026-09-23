"""Harness events: the loop's output is a stream, not a print.

Every consumer of a run is a **reducer over this stream** — the CLI printer now, the TUI
later (docs/architecture/aiden-architecture.md §10: "The UI consumes events, never state;
it may not mutate anything except by enqueuing a command"). That is why the loop never
calls ``print`` itself: doing so would make the TUI a rewrite instead of a projection.

These are *harness* events, distinct from the provider ``StreamEvent`` vocabulary in
``aiden.providers.types``. The loop translates provider events plus tool executions into
the events below, which are the ones a UI or a log cares about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .providers.types import Usage

# --------------------------------------------------------------------------- events


@dataclass(slots=True)
class RunStarted:
    session_id: str
    model: str
    question: str
    cwd: str


@dataclass(slots=True)
class TurnStarted:
    turn: int


@dataclass(slots=True)
class TextDelta:
    text: str


@dataclass(slots=True)
class ThinkingDelta:
    text: str


@dataclass(slots=True)
class ToolCallStarted:
    call_id: str
    name: str
    arguments: dict


@dataclass(slots=True)
class ToolCallFinished:
    call_id: str
    name: str
    is_error: bool
    duration_ms: int
    output_chars: int
    #: Full tool output. Renderers cap what they *show* (the spec's visible-line budget) but the
    #: text must be here, or the transcript shows a preview masquerading as the whole result.
    output: str = ""
    truncated: bool = False
    #: True when the provider truncated the arguments, so the call was never executed.
    skipped: bool = False


@dataclass(slots=True)
class Diagnostic:
    """A cap or budget left a trace.

    research/02 §3: "Every cap must leave a trace, or the model will reason over a hole it
    cannot see." Diagnostics are surfaced, never swallowed.
    """

    message: str
    level: str = "warning"


@dataclass(slots=True)
class ApprovalRequested:
    """A mutating tool wants to change the working tree.

    Rendered as a diff before the decision, because an approval prompt that shows only "allow?" is
    a prompt that trains the user to say yes to nothing (research/06 §What great agent UIs do #9).
    """

    call_id: str
    name: str
    path: str
    diff: str
    sensitive: bool = False
    reason: str = ""


@dataclass(slots=True)
class ApprovalResolved:
    call_id: str
    approved: bool
    #: How the decision was made: "user", "auto" (non-interactive default), or "flag".
    decided_by: str = "user"
    note: str = ""


@dataclass(slots=True)
class UsageUpdated:
    usage: Usage
    cost_usd: float


@dataclass(slots=True)
class RunFinished:
    stop_reason: str
    turns: int
    tool_calls: int
    usage: Usage
    cost_usd: float
    session_path: str = ""
    answer: str = ""
    error: str = ""


Event = (
    ApprovalRequested
    | ApprovalResolved
    | RunStarted
    | TurnStarted
    | TextDelta
    | ThinkingDelta
    | ToolCallStarted
    | ToolCallFinished
    | Diagnostic
    | UsageUpdated
    | RunFinished
)


# --------------------------------------------------------------------------- sinks


class EventSink(Protocol):
    """Anything that renders or records a run."""

    def emit(self, event: Event) -> None: ...


class NullSink:
    """Discards events. Useful in tests that only assert on the result."""

    def emit(self, event: Event) -> None:
        return None


@dataclass
class RecordingSink:
    """Collects events for assertions and for the future golden-frame TUI tests."""

    events: list[Event] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def of[T: Event](self, kind: type[T]) -> list[T]:
        return [e for e in self.events if isinstance(e, kind)]

    @property
    def text(self) -> str:
        """All assistant text, as a user would read it."""
        return "".join(e.text for e in self.events if isinstance(e, TextDelta))
