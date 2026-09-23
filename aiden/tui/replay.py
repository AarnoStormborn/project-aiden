"""Replay a recorded session through the TUI.

The architecture's central claim is that the transcript is a *projection* of the session log
(docs/architecture/aiden-architecture.md §7): the TUI, the provider request, resume and fork are
all views of the same entries. This module is the test of that claim — it turns a log back into
the event stream the driver already knows how to render, so a recorded session looks exactly like
it did live.

It is also the foundation for `aiden tui --resume`, which needs the same adapter to seed a
continuing conversation.
"""

from __future__ import annotations

from pathlib import Path

from ..events import (
    Diagnostic,
    Event,
    RunFinished,
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from ..providers.types import Usage
from ..session import (
    ENTRY_ASSISTANT,
    ENTRY_DIAGNOSTIC,
    ENTRY_RUN_END,
    ENTRY_SESSION,
    ENTRY_TOOL_RESULT,
    ENTRY_USER,
    Entry,
    read_entries,
)


def events_from_entries(entries: list[Entry], *, cwd: str = "") -> list[Event]:
    """Convert session entries into the event stream the driver renders."""
    events: list[Event] = []
    header = next((e for e in entries if e.type == ENTRY_SESSION), None)
    model = (header.payload.get("model", "") if header else "") or "unknown"
    session_id = (header.payload.get("id", "") if header else "") or "replay"
    where = (header.payload.get("cwd", "") if header else "") or cwd
    # Attribute each tool result to the turn that requested it, so a result arriving after a
    # user message is not mis-parented.
    pending_turns = 0

    for entry in entries:
        payload = entry.payload
        if entry.type == ENTRY_USER:
            # Each question begins a new run, which is how the live loop behaves.
            events.append(
                RunStarted(
                    session_id=session_id,
                    model=model,
                    question=payload.get("text", ""),
                    cwd=where,
                )
            )
            pending_turns += 1
        elif entry.type == ENTRY_ASSISTANT:
            # Order matters for reconstruction: reasoning, then prose, then the action it leads to.
            # Emitting the tool calls first put the assistant's text *after* the tool cell, which
            # reads as if it spoke only after seeing the result.
            if payload.get("thinking"):
                events.append(ThinkingDelta(text=payload["thinking"]))
            if payload.get("text"):
                events.append(TextDelta(text=payload["text"]))
            for call in payload.get("tool_calls") or []:
                events.append(
                    ToolCallStarted(
                        call_id=call.get("id", ""),
                        name=call.get("name", ""),
                        arguments=call.get("arguments") or {},
                    )
                )
        elif entry.type == ENTRY_TOOL_RESULT:
            events.append(
                ToolCallFinished(
                    call_id=payload.get("call_id", ""),
                    name=payload.get("name", ""),
                    is_error=bool(payload.get("is_error")),
                    duration_ms=int(payload.get("duration_ms", 0) or 0),
                    output_chars=len(payload.get("output", "") or ""),
                    output=payload.get("output", "") or "",
                    truncated=bool(payload.get("truncated")),
                    skipped=bool(payload.get("skipped")),
                )
            )
        elif entry.type == ENTRY_DIAGNOSTIC:
            events.append(
                Diagnostic(
                    message=payload.get("message", ""), level=payload.get("level", "warning")
                )
            )
        elif entry.type == ENTRY_RUN_END:
            events.append(
                RunFinished(
                    stop_reason=payload.get("stop_reason", "end_turn"),
                    turns=int(payload.get("turns", 0) or 0),
                    tool_calls=int(payload.get("tool_calls", 0) or 0),
                    usage=_usage(payload.get("usage")),
                    cost_usd=float(payload.get("cost_usd", 0.0) or 0.0),
                    answer=payload.get("answer", "") or "",
                    error=payload.get("error", "") or "",
                )
            )
            pending_turns = max(0, pending_turns - 1)

    return events


def _usage(raw: object) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    return Usage(
        input_tokens=int(raw.get("input_tokens", 0) or 0),
        output_tokens=int(raw.get("output_tokens", 0) or 0),
        cache_read_tokens=int(raw.get("cache_read_tokens", 0) or 0),
        cache_write_tokens=int(raw.get("cache_write_tokens", 0) or 0),
        reasoning_tokens=int(raw.get("reasoning_tokens", 0) or 0),
    )


def events_from_session(path: Path, *, cwd: str = "") -> list[Event]:
    return events_from_entries(list(read_entries(path)), cwd=cwd)


def questions_from_session(path: Path) -> list[str]:
    """The user's questions, in order. Used to seed a resumed conversation."""
    return [
        entry.payload.get("text", "")
        for entry in read_entries(path)
        if entry.type == ENTRY_USER and entry.payload.get("text")
    ]
