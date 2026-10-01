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
from typing import Any

from . import config, prompts
from .approval import Approver, DenyAll
from .checkpoint import Checkpoint, CheckpointStore
from .context import (
    SUMMARY_MAX_TOKENS,
    apply_compaction,
    cut_index,
    estimate_tokens,
    needs_compaction,
    render_for_summary,
    summarise,
    summary_prompt,
)
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
    ENTRY_COMPACTION,
    ENTRY_DIAGNOSTIC,
    ENTRY_RUN_END,
    ENTRY_TOOL_RESULT,
    ENTRY_USAGE,
    ENTRY_USER,
    SessionStore,
)
from .tools import (
    GATED_TOOLS,
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

    previous_summary = ""
    compactions = 0

    try:
        for turn in range(1, max_turns + 1):
            result.turns = turn
            sink.emit(TurnStarted(turn=turn))

            # Request-time compaction, checked before *every* request rather than only between turns.
            # Long runs are where context dies, and the check has to happen after tool results are
            # appended and before the next request — which is exactly here ([r00] §3).
            if compactions < config.MAX_COMPACTIONS and needs_compaction(
                messages, system, info.context_window
            ):
                compacted = await _compact(
                    suite,
                    info,
                    messages,
                    result,
                    sink,
                    session,
                    reason="above the window reserve",
                    previous_summary=previous_summary,
                    tail_budget_tokens=_usable_window(info),
                )
                if compacted is not None:
                    messages, previous_summary = compacted
                    compactions += 1

            completion = await _complete(suite, info, messages, system, max_tokens, thinking_level)
            _record_completion(session, completion, result, sink)

            if completion.stop_reason == "error":
                # An overflow is the one error the loop itself can recover from: summarise and resend.
                # Everything else ends the run, because retrying the same bytes cannot help. The
                # classification comes from the provider layer; the decision is the loop's.
                if completion.error_kind == "overflow" and compactions < config.MAX_COMPACTIONS:
                    compacted = await _compact(
                        suite,
                        info,
                        messages,
                        result,
                        sink,
                        session,
                        reason="the provider refused the request as too long",
                        previous_summary=previous_summary,
                        tail_budget_tokens=_usable_window(info),
                    )
                    if compacted is not None:
                        messages, previous_summary = compacted
                        compactions += 1
                        continue
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
    tools: list[Any] | None = None,
) -> Completion:
    return await suite.complete(
        info,
        messages,
        tool_specs() if tools is None else tools,
        system=system,
        max_tokens=max_tokens,
        thinking_level=thinking_level,
    )


async def _compact(
    suite: ProviderSuite,
    info: ModelInfo,
    messages: list[Message],
    result: LoopResult,
    sink: EventSink,
    session: SessionStore,
    *,
    reason: str,
    previous_summary: str,
    tail_budget_tokens: int | None = None,
) -> tuple[list[Message], str] | None:
    """Replace the oldest part of the context with a summary.

    Returns the new message list and the summary to pass on as iterative context, or ``None`` when
    compaction cannot help — in which case the caller keeps its original error rather than reporting
    a rescue that did not happen.
    """
    cut = cut_index(messages, tail_budget_tokens=tail_budget_tokens)
    if cut <= 0:
        # Nothing to elide. Reporting a compaction here would claim a saving that did not occur.
        return None
    # Compacting a prefix smaller than the largest possible summary makes the context *bigger*: the
    # summary replaces less than it costs. Declining costs nothing and keeps the request honest.
    if estimate_tokens(messages[:cut]) <= SUMMARY_MAX_TOKENS:
        note = (
            f"not compacting: only {estimate_tokens(messages[:cut])} tokens would be replaced, which "
            f"is below the {SUMMARY_MAX_TOKENS}-token summary budget, so the context would grow"
        )
        result.diagnostics.append(note)
        sink.emit(Diagnostic(message=note, level="info"))
        session.append(ENTRY_DIAGNOSTIC, {"message": note, "level": "info"})
        return None

    prompt = summary_prompt(previous_summary, render_for_summary(messages[:cut]))
    try:
        # No tools: the summariser is asked for prose, and giving it tools invites a call that would
        # be executed against the very context being replaced.
        completion = await _complete(
            suite, info, [Message.text("user", prompt)], "", SUMMARY_MAX_TOKENS, None, tools=[]
        )
    except Exception as exc:
        _compaction_failed(result, sink, session, f"{type(exc).__name__}: {exc}")
        return None

    # A summary is real spend, so it joins the run's totals rather than sitting beside them.
    if completion.usage.total_tokens or completion.cost_usd:
        result.usage = result.usage + completion.usage
        result.cost_usd += completion.cost_usd
        sink.emit(UsageUpdated(usage=result.usage, cost_usd=result.cost_usd))
        session.append(
            ENTRY_USAGE,
            {**_usage_dict(completion.usage), "cost_usd": round(completion.cost_usd, 6)},
        )

    compaction = summarise(completion, messages, cut, previous_summary=previous_summary)
    if compaction.tokens_after >= compaction.tokens_before:
        # The summary cost money already, but applying a compaction that does not shrink the context
        # would make the next request *more* likely to overflow while looking like a fix. Refuse, and
        # say so rather than reporting a saving that did not happen.
        note = (
            f"compaction declined: it would not have shrunk the context "
            f"({compaction.tokens_before} -> {compaction.tokens_after} estimated tokens)"
        )
        result.diagnostics.append(note)
        sink.emit(Diagnostic(message=note, level="info"))
        session.append(ENTRY_DIAGNOSTIC, {"message": note, "level": "info"})
        return None
    new_messages = apply_compaction(messages, compaction)

    note = (
        f"context compacted ({reason}): {compaction.tokens_before} -> {compaction.tokens_after} "
        f"estimated tokens, {compaction.first_kept_index} messages summarised"
        + (
            " (mechanical placeholder, the summariser returned nothing)"
            if compaction.fallback
            else ""
        )
    )
    result.diagnostics.append(note)
    level = "warning" if compaction.fallback else "info"
    sink.emit(Diagnostic(message=note, level=level))
    # Logged, because a compaction is the one event that makes the model's context diverge from the
    # transcript: without it a reader cannot tell why the agent stopped knowing something.
    session.append(
        ENTRY_COMPACTION,
        {
            "summary": compaction.summary,
            "first_kept_entry_id": compaction.first_kept_index,
            "retained_tail": False,
            "tokens_before": compaction.tokens_before,
            "tokens_after": compaction.tokens_after,
            "reason": reason,
            "fallback": compaction.fallback,
        },
    )
    return new_messages, compaction.summary


def _usable_window(info: ModelInfo) -> int:
    """Tokens a request may occupy: the model's window minus the room its reply needs.

    Floored at 1 so a tiny or unknown window degrades to "keep as little as possible" rather than to
    a negative budget, which would make the cut arithmetic meaningless.
    """
    return max(1, info.context_window - config.RESERVE_TOKENS)


def _compaction_failed(
    result: LoopResult, sink: EventSink, session: SessionStore, detail: str
) -> None:
    """Record a summariser failure loudly and decline to compact.

    Declining is the honest choice: the alternative is dropping the prefix with nothing in its place,
    which is the silent truncation this project names as a failure mode. The run then stops on its
    original error, which is visible.
    """
    note = f"compaction failed, context left unchanged: {detail}"
    result.diagnostics.append(note)
    sink.emit(Diagnostic(message=note, level="error"))
    session.append(ENTRY_DIAGNOSTIC, {"message": note, "level": "error"})
    return None


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
    if call.name not in GATED_TOOLS:
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
    # Not every gated tool names a path. A shell is identified by its command and a fetch by its
    # URL; without those fallbacks the prompt asked "apply this change to ?".
    target = path or str(call.arguments.get("command") or call.arguments.get("url") or "")

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
