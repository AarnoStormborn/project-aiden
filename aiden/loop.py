"""The agent loop (L3).

Adopts Pi's shape — the smallest correct one — with the two non-negotiables the research
established (docs/architecture/aiden-architecture.md §2):

1. **``stop_reason == "max_tokens"`` fails every tool call in that message.** Streamed
   arguments may be silently truncated, and executing them is worse than reporting an error.
2. **Explicit turn and cost ceilings**, enforced *inside* the loop so it can stop cleanly and
   report, rather than being killed from outside ([r01] §loop).

v0.1 executes tool calls sequentially. They are all read-only, but determinism keeps the
transcript reproducible and the prompt cache warm, which matters more here than latency.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, prompts
from .approval import Approver, DenyAll
from .checkpoint import Checkpoint, CheckpointStore
from .events import (
    ApprovalRequested,
    ApprovalResolved,
    Diagnostic,
    EventSink,
    NullSink,
    RunFinished,
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnStarted,
    UsageUpdated,
)
from .providers import Message, ProviderSuite, ToolResultPart
from .providers.types import Completion, ModelInfo, Part, ToolCallPart, Usage
from .session import (
    ENTRY_APPROVAL,
    ENTRY_ASSISTANT,
    ENTRY_DIAGNOSTIC,
    ENTRY_RUN_END,
    ENTRY_TOOL_RESULT,
    ENTRY_USAGE,
    ENTRY_USER,
    SessionStore,
)
from .tools import (
    MUTATING_TOOLS,
    TOOLS,
    ToolContext,
    ToolResult,
    execute,
    preview_of,
    tool_specs,
)


@dataclass(slots=True)
class LoopResult:
    """What a run produced, for the CLI to print and for tests to assert on."""

    answer: str = ""
    stop_reason: str = "end_turn"
    turns: int = 0
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    session_path: str = ""
    diagnostics: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.stop_reason != "error"


async def run_loop(
    question: str,
    *,
    suite: ProviderSuite,
    model: ModelInfo | str,
    cwd: Path | None = None,
    sink: EventSink | None = None,
    session: SessionStore | None = None,
    max_turns: int = config.MAX_TURNS,
    max_cost_usd: float = config.MAX_RUN_COST_USD,
    max_tokens: int = config.DEFAULT_MAX_TOKENS,
    thinking_level: str | None = config.DEFAULT_THINKING_LEVEL,
    system_prompt: str | None = None,
    approver: Approver | None = None,
    checkpoints: CheckpointStore | None = None,
) -> LoopResult:
    """Ask a question about the repository and return the answer plus a cost report."""
    sink = sink or NullSink()
    cwd = (cwd or Path.cwd()).resolve()
    info = suite.resolve(model) if isinstance(model, str) else model

    owned_session = session is None
    session = session or SessionStore.create(cwd=cwd, model=info.ref)
    ctx = ToolContext(cwd=cwd, spill_dir=config.SPILL_DIR)
    # Mutating tools are refused unless something explicitly allows them: a run with nobody to ask
    # must not be able to write (aiden/approval.py).
    resolved_approver: Approver = approver or DenyAll()
    checkpoint_store = checkpoints or CheckpointStore(session.session_id, cwd=cwd)
    checkpoint: Checkpoint | None = None
    checkpoint_taken_for: int = -1

    result = LoopResult(session_path=str(session.path))
    sink.emit(
        RunStarted(
            session_id=session.session_id,
            model=info.ref,
            question=question,
            cwd=str(cwd),
        )
    )
    session.append(
        ENTRY_USER,
        {"text": question, "system_prompt_hash": prompts.base_prompt_hash()},
    )

    messages: list[Message] = [Message.text("user", question)]
    system = system_prompt or prompts.build(cwd)

    try:
        for turn in range(1, max_turns + 1):
            result.turns = turn
            sink.emit(TurnStarted(turn=turn))

            completion = await _complete(suite, info, messages, system, max_tokens, thinking_level)
            _record_completion(session, completion, result, sink)

            if completion.stop_reason == "error":
                result.error = completion.diagnostic or completion.error or "provider error"
                result.stop_reason = "error"
                break

            calls = completion.tool_calls
            if not calls:
                result.answer = completion.text.strip()
                result.stop_reason = completion.stop_reason
                break

            # Rule 1: truncated tool arguments are never executed. But every call must still be
            # answered, because providers require one tool_result per tool_use — dropping a call
            # would leave an orphaned tool_use and break the next request.
            messages.append(completion.to_message())
            tool_results: list[Part] = []
            for call in calls:
                if call.truncated:
                    tool_results.append(_skipped_result(call, sink, session))
                    continue
                # A checkpoint covers the whole turn, taken lazily before its first mutation, so
                # undo restores the state before the turn rather than before the last edit.
                if call.name in MUTATING_TOOLS and checkpoint_taken_for != turn:
                    checkpoint = checkpoint_store.begin(turn)
                    checkpoint_store.prune(config.CHECKPOINT_KEEP)
                    checkpoint_taken_for = turn
                tool_results.append(
                    await _execute_one(
                        call,
                        ctx,
                        result,
                        sink,
                        session,
                        approver=resolved_approver,
                        checkpoint_store=checkpoint_store,
                        checkpoint=checkpoint,
                    )
                )
            messages.append(Message(role="tool", content=tool_results))

            # Budget awareness: near the ceiling, tell the model to answer with what it has.
            # Without this, a thorough agent keeps verifying and hits max_turns with nothing.
            #
            # The threshold is capped at half the budget. A fixed threshold equal to the whole
            # budget fires on turn 1 of a short run, which suppressed exploration entirely: with
            # max_turns=3 the agent was told to wrap up before it had read anything, and answered
            # "I did not read them" to a question it could have answered in one more call.
            remaining = max_turns - turn
            nudge_at = max(1, min(config.NUDGE_TURNS_REMAINING, max_turns // 2))
            if 0 < remaining <= nudge_at:
                notice = _wrap_up_notice(remaining)
                messages.append(Message.text("user", notice))
                # Recorded, not silent: the transcript must explain why the agent stopped
                # looking, and a harness intervention is part of the run's history.
                sink.emit(Diagnostic(message=notice, level="info"))
                session.append(ENTRY_DIAGNOSTIC, {"message": notice, "level": "info"})

            if result.cost_usd >= max_cost_usd:
                note = (
                    f"cost ceiling reached (${result.cost_usd:.4f} of ${max_cost_usd:.2f}); "
                    "stopping before the next turn"
                )
                result.diagnostics.append(note)
                sink.emit(Diagnostic(message=note, level="error"))
                session.append(ENTRY_DIAGNOSTIC, {"message": note, "level": "error"})
                result.stop_reason = "cost_limit"
                break
        else:
            note = f"turn ceiling reached ({max_turns} turns) without a final answer"
            result.diagnostics.append(note)
            sink.emit(Diagnostic(message=note, level="error"))
            session.append(ENTRY_DIAGNOSTIC, {"message": note, "level": "error"})
            result.stop_reason = "max_turns"
    except (asyncio.CancelledError, KeyboardInterrupt):
        # An interrupted run must not be recorded as having ended normally. The log is the record
        # of what happened, and `end_turn` would make a cancelled run look complete on replay —
        # which is exactly how this was noticed: a replay of an interrupted session showed no sign
        # that it had been interrupted. The driver has already committed the partial answer.
        result.stop_reason = "aborted"
        raise
    finally:
        session.append(
            ENTRY_RUN_END,
            {
                "stop_reason": result.stop_reason,
                "turns": result.turns,
                "tool_calls": result.tool_calls,
                "usage": _usage_dict(result.usage),
                "cost_usd": round(result.cost_usd, 6),
                "answer": result.answer,
                "error": result.error,
            },
        )
        if owned_session:
            session.close()

    sink.emit(
        RunFinished(
            stop_reason=result.stop_reason,
            turns=result.turns,
            tool_calls=result.tool_calls,
            usage=result.usage,
            cost_usd=result.cost_usd,
            session_path=result.session_path,
            answer=result.answer,
            error=result.error,
        )
    )
    return result


# --------------------------------------------------------------------------- internals


async def _complete(
    suite: ProviderSuite,
    info: ModelInfo,
    messages: list[Message],
    system: str,
    max_tokens: int,
    thinking_level: str | None,
) -> Completion:
    return await suite.complete(
        info,
        messages,
        tool_specs(),
        system=system,
        max_tokens=max_tokens,
        thinking_level=thinking_level,
    )


def _record_completion(
    session: SessionStore,
    completion: Completion,
    result: LoopResult,
    sink: EventSink,
) -> None:
    """Stream the assistant turn out and persist it."""
    if completion.thinking:
        sink.emit(ThinkingDelta(text=completion.thinking))
    if completion.text:
        sink.emit(TextDelta(text=completion.text))

    session.append(
        ENTRY_ASSISTANT,
        {
            "text": completion.text,
            "thinking": completion.thinking,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments}
                for c in completion.tool_calls
            ],
            "stop_reason": completion.stop_reason,
        },
    )

    if completion.usage.total_tokens or completion.cost_usd:
        result.usage = result.usage + completion.usage
        result.cost_usd += completion.cost_usd
        sink.emit(UsageUpdated(usage=result.usage, cost_usd=result.cost_usd))
        session.append(
            ENTRY_USAGE,
            {**_usage_dict(completion.usage), "cost_usd": round(completion.cost_usd, 6)},
        )

    # Caps leave traces: surface truncation / reasoning-exhaustion diagnostics.
    if completion.diagnostic:
        result.diagnostics.append(completion.diagnostic)
        sink.emit(Diagnostic(message=completion.diagnostic))
        session.append(ENTRY_DIAGNOSTIC, {"message": completion.diagnostic, "level": "warning"})


def _skipped_result(call: ToolCallPart, sink: EventSink, session: SessionStore) -> ToolResultPart:
    """Report a truncated call as an error result instead of executing it (rule 1).

    It still returns a result for the call: providers require one tool_result per tool_use,
    so dropping it would leave an orphaned tool_use and break the next request.
    """
    message = (
        f"tool call '{call.name}' was not executed: its arguments were truncated "
        "(the response hit the output limit). Re-issue it in a smaller step."
    )
    sink.emit(
        ToolCallFinished(
            call_id=call.id,
            name=call.name,
            is_error=True,
            duration_ms=0,
            output_chars=0,
            skipped=True,
        )
    )
    sink.emit(Diagnostic(message=message))
    session.append(
        ENTRY_TOOL_RESULT,
        {
            "call_id": call.id,
            "name": call.name,
            "is_error": True,
            "skipped": True,
            "output": message,
        },
    )
    return ToolResultPart(call_id=call.id, output=message, is_error=True)


async def _execute_one(
    call: ToolCallPart,
    ctx: ToolContext,
    result: LoopResult,
    sink: EventSink,
    session: SessionStore,
    *,
    approver: Approver,
    checkpoint_store: CheckpointStore | None = None,
    checkpoint: Checkpoint | None = None,
) -> ToolResultPart:
    """Run one tool call, emit its events, record it, and return its result.

    Called in call order (not completion order) so the transcript stays deterministic and the
    prompt cache stays warm ([r01] §tools).

    A mutating tool goes through the rest of the chain first — preview, checkpoint, approval — and
    only then executes. Nothing about that ordering is incidental: the checkpoint must exist before
    the change, and the approval must see the diff before the decision.
    """
    result.tool_calls += 1
    sink.emit(ToolCallStarted(call_id=call.id, name=call.name, arguments=call.arguments))

    rejection = await _gate_mutation(
        call,
        ctx,
        sink,
        session,
        approver=approver,
        checkpoint_store=checkpoint_store,
        checkpoint=checkpoint,
    )

    started = time.perf_counter()
    tool_result = rejection if rejection is not None else execute(call.name, call.arguments, ctx)
    duration_ms = int((time.perf_counter() - started) * 1000)
    rendered = tool_result.render()

    sink.emit(
        ToolCallFinished(
            call_id=call.id,
            name=call.name,
            is_error=tool_result.is_error,
            duration_ms=duration_ms,
            output_chars=len(rendered),
            output=rendered,
            truncated=tool_result.truncated,
        )
    )
    session.append(
        ENTRY_TOOL_RESULT,
        {
            "call_id": call.id,
            "name": call.name,
            "is_error": tool_result.is_error,
            "truncated": tool_result.truncated,
            "duration_ms": duration_ms,
            "output": rendered,
        },
    )
    return ToolResultPart(call_id=call.id, output=rendered, is_error=tool_result.is_error)


async def _gate_mutation(
    call: ToolCallPart,
    ctx: ToolContext,
    sink: EventSink,
    session: SessionStore,
    *,
    approver: Approver,
    checkpoint_store: CheckpointStore | None,
    checkpoint: Checkpoint | None,
) -> ToolResult | None:
    """Policy, checkpoint and approval for a mutating call.

    Returns a rejection result when the change must not happen, or ``None`` to proceed. A no-op
    preview means the tool is about to refuse anyway (bad path, missing match, stale read), so there
    is nothing to ask a human about — the tool's own error result is the right answer.
    """
    if call.name not in MUTATING_TOOLS:
        return None

    tool = TOOLS[call.name]
    # A tool may decide per call that this particular invocation is provably harmless — `bash` does
    # exactly that for read-only commands. The default is to ask.
    needs_approval = getattr(tool, "approval_required", None)
    if callable(needs_approval) and not needs_approval(call.arguments, ctx):
        return None

    diff = preview_of(tool, call.arguments, ctx)
    if diff is None:
        return None

    # The *path* policy applies only to tools that name a path. Applying it to every mutating tool
    # refused `bash` with "path is empty" before the command policy ever ran — the guard was right
    # about the shape of a file tool and wrong about the shape of a shell.
    path = str(call.arguments.get("path", ""))
    sensitive = False
    if path:
        _resolved, decision = ctx.policy().check(path, ctx.cwd)
        if not decision.allowed:
            return ToolResult.error(decision.reason)
        sensitive = decision.sensitive
    # A shell has no path, so the command is what the prompt identifies.
    target = path or str(call.arguments.get("command", ""))

    reason_text = str(call.arguments.get("reason") or call.arguments.get("description") or "")
    sink.emit(
        ApprovalRequested(
            call_id=call.id,
            name=call.name,
            path=target,
            diff=diff,
            sensitive=sensitive,
            reason=reason_text,
        )
    )
    verdict = await approver.request(
        name=call.name,
        path=target,
        diff=diff,
        sensitive=sensitive,
        reason=reason_text,
    )
    sink.emit(
        ApprovalResolved(
            call_id=call.id,
            approved=verdict.approved,
            decided_by=verdict.decided_by,
            note=verdict.note,
        )
    )
    session.append(
        ENTRY_APPROVAL,
        {
            "call_id": call.id,
            "name": call.name,
            "path": target,
            "approved": verdict.approved,
            "decided_by": verdict.decided_by,
            "note": verdict.note,
            "diff_lines": len(diff.splitlines()),
        },
    )

    if verdict.approved:
        # Checkpoint after the decision but before the change. Capturing earlier also works, but it
        # leaves empty checkpoints for every declined proposal, and `/undo` then walks through turns
        # that changed nothing.
        if checkpoint is not None and checkpoint_store is not None and path:
            resolved, _ = ctx.policy().check(path, ctx.cwd)
            if resolved is not None:
                checkpoint_store.capture(checkpoint, resolved)
        return None

    # The reason travels with the refusal so the model can adapt rather than retry identically.
    reason = verdict.note or "the user declined this change"
    return ToolResult.error(
        f"{call.name} on {target} was declined and NOT run: {reason}\n"
        "Do not retry the same change. Ask what to do differently, or propose an alternative."
    )


def _wrap_up_notice(remaining: int) -> str:
    plural = "turn" if remaining == 1 else "turns"
    return (
        f"[harness] You have {remaining} {plural} left before the run is cut off. "
        "Stop searching and give your final answer now, using what you have already read. "
        "If something is unresolved, say so in one line rather than continuing to look."
    )


def _usage_dict(usage: Usage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
    }
