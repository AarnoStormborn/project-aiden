"""Replacing a prefix of the context with a structured summary.

A compilation rule matters more than the prose: **the cut point is never between a tool call and its
result.** Providers require one result per call, so a cut that separates them produces a request that
is rejected outright — the compaction would break the run it was rescuing. ``cut_index`` therefore
walks the boundary backwards to a safe place rather than trusting the token count.

The summary is a *summary*, not a transcript: it states the goal, what is settled, what is in flight,
and what is next, because that is what a later turn needs and what a verbatim prefix does not fit.
The previous summary is passed in as context, so compacting twice keeps the first summary's content
instead of losing it ([r00] §3, iterative summarisation).
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import config
from ..providers.types import Completion, Message, TextPart, ToolCallPart, ToolResultPart
from .estimate import estimate_tokens, should_compact

#: The structure the summary must follow. Fixed because the sections are what a later turn reads:
#: a free-form summary reliably omits the constraints and the blocked work, which are the two things
#: a resuming agent gets wrong.
SUMMARY_INSTRUCTIONS = """\
Summarise the conversation so far so that work can continue without the original messages.

Use exactly these headings, and keep each to a few lines:

Goal: what the user asked for, in their words where possible.
Constraints: rules, preferences and limits that must keep holding.
Progress: what is done, what is in flight, what is blocked.
Key decisions: choices already made, with the reason, so they are not relitigated.
Next steps: the immediate next actions, in order.
Files touched: paths changed or read, with what changed.

Write facts, not narration. Do not describe the process of summarising. Do not greet. If something
is unknown, say unknown rather than guessing. Keep paths exact."""

#: Room for the summary itself. Small on purpose: a long summary of an overflowing context is its own
#: problem, and the structure above bounds what is needed.
SUMMARY_MAX_TOKENS = 1_500


@dataclass(slots=True)
class Compaction:
    """The record of one compaction, and what it did."""

    summary: str
    #: Index into the pre-compaction message list: everything before it was replaced.
    first_kept_index: int
    #: Estimated tokens before and after, so the saving is visible rather than assumed.
    tokens_before: int
    tokens_after: int
    #: True when the model could not be reached and a mechanical note stood in for a summary.
    fallback: bool = False


def cut_index(
    messages: list[Message],
    keep_recent_tokens: int = config.KEEP_RECENT_TOKENS,
    *,
    tail_budget_tokens: int | None = None,
) -> int:
    """Where to split, keeping roughly ``keep_recent_tokens`` of the newest messages.

    Walks back from the newest message accumulating estimates until the budget is spent, then moves
    the boundary *backwards* while it would split a tool result from the call it answers. The loop
    appends an assistant message holding the calls and then a tool message holding the results, so
    the only unsafe boundary is one landing on a tool message; moving to the preceding assistant
    message keeps the pair together.

    ``tail_budget_tokens`` is a hard ceiling derived from the model's window and it wins over
    ``keep_recent_tokens``: on a small window the keep-recent target can exceed the room available,
    in which case a tail that "fits" the target does not fit the request, and the cut must move.

    The forced cut below compares against the *effective target*, not the raw budget. That is what
    makes the overflow path work: a provider can reject a request as too long while the catalog's
    window says it fits (the catalog can be wrong, or the provider can count differently), and in
    that case the keep-recent target is the only bound that produces a smaller request.
    """
    if not messages:
        return 0
    target = (
        keep_recent_tokens
        if tail_budget_tokens is None
        else min(keep_recent_tokens, max(1, tail_budget_tokens))
    )
    total = 0
    cut = len(messages)
    for index in range(len(messages) - 1, -1, -1):
        total += estimate_tokens([messages[index]])
        cut = index
        if total >= target:
            break
    # Never split a call from its result. A tool message directly after the cut means its assistant
    # message would be dropped and the request would carry an orphaned result.
    while 0 < cut < len(messages) and messages[cut].role == "tool":
        cut -= 1
    # A cut of 0 elides nothing, so the compaction would be a no-op. When a budget was supplied *and
    # the context actually exceeds it*, the caller needs the prefix gone, so elide the oldest messages
    # and move forward to the first boundary that does not orphan a tool result. Gated on the budget
    # being exceeded, because otherwise a large window would force a cut on a context that already
    # fits. Without the forcing, a small window made every compaction silently do nothing — the
    # trigger fired and the cut refused, forever.
    if (
        cut == 0
        and tail_budget_tokens is not None
        and len(messages) > 1
        and estimate_tokens(messages) > target
    ):
        cut = 1
        while cut < len(messages) - 1 and messages[cut].role == "tool":
            cut += 1
    return cut


def needs_compaction(messages: list[Message], system: str, context_window: int) -> bool:
    """The request-time predicate, run before a request is sent."""
    return should_compact(estimate_tokens(messages, system), context_window)


def render_for_summary(messages: list[Message]) -> str:
    """Flatten messages into text for the summariser, keeping the roles visible."""
    lines: list[str] = []
    for message in messages:
        for part in message.content:
            if isinstance(part, TextPart) and part.text.strip():
                lines.append(f"[{message.role}] {part.text.strip()}")
            elif isinstance(part, ToolCallPart):
                lines.append(f"[tool call] {part.name} {part.raw_arguments or part.arguments}")
            elif isinstance(part, ToolResultPart):
                output = part.output.strip()
                if output:
                    lines.append(f"[tool result] {output}")
    return "\n".join(lines)


def summary_prompt(previous_summary: str, transcript: str) -> str:
    """The summariser's instruction, with the prior summary as iterative context."""
    previous = (
        f"\nAn earlier summary of the conversation before this one:\n\n{previous_summary}\n"
        if previous_summary.strip()
        else ""
    )
    return f"{SUMMARY_INSTRUCTIONS}\n{previous}\nThe conversation to summarise:\n\n{transcript}"


def mechanical_note(elided: list[Message], kept: list[Message]) -> str:
    """What stands in for a summary when the summariser could not be reached.

    Stated as mechanical rather than dressed up as a summary: an agent that believes this is a real
    summary will trust gaps it should not. The alternative — dropping the prefix silently — is the
    named failure mode this project refuses.
    """
    goal = ""
    for message in elided:
        for part in message.content:
            if isinstance(part, TextPart) and message.role == "user" and part.text.strip():
                goal = part.text.strip()[:400]
                break
        if goal:
            break
    return (
        "Goal: (unknown — this is a mechanical placeholder, not a model summary)\n"
        f"Constraints: (unknown)\nProgress: {len(elided)} earlier messages were removed to fit the "
        f"context window and could not be summarised, because the summariser call failed.\n"
        f"Key decisions: (unknown — treat earlier decisions as lost and re-check them)\n"
        f"Next steps: continue from the {len(kept)} messages that follow this note.\n"
        "Files touched: (unknown)\n\n"
        f"The original request began: {goal or '(not found)'}"
    )


def compaction_message(compaction: Compaction) -> Message:
    """The message that represents the replaced prefix.

    A ``user`` message rather than an assistant one: it is information the model is given, not
    something the model said, and a provider that validates assistant turns should never see a
    fabricated one.
    """
    label = "context summary (mechanical)" if compaction.fallback else "context summary"
    return Message.text(
        "user",
        f"<{label}>\n{compaction.summary}\n</{label}>\n"
        f"({compaction.tokens_before} tokens were replaced by {compaction.tokens_after}; "
        f"the {compaction.first_kept_index} most recent messages are kept verbatim below.)",
    )


def apply_compaction(messages: list[Message], compaction: Compaction) -> list[Message]:
    """The new message list: the summary, then the kept tail."""
    return [compaction_message(compaction), *messages[compaction.first_kept_index :]]


def summarise(
    completion: Completion,
    messages: list[Message],
    cut: int,
    *,
    previous_summary: str = "",
) -> Compaction:
    """Build a ``Compaction`` from a summariser's completion.

    Kept separate from the provider call so the loop owns the call's usage and cost — a summary is
    real spend, and it belongs in the run's totals rather than beside them.
    """
    elided = messages[:cut]
    kept = messages[cut:]
    text = completion.text.strip()
    fallback = not text
    body = text or mechanical_note(elided, kept)
    label = "context summary (mechanical)" if fallback else "context summary"
    # Estimated on a stand-in message, so the figure reflects what the next request actually sends
    # rather than a guess at the summary's size.
    stand_in = Message.text("user", f"<{label}>\n{body}\n</{label}>")
    return Compaction(
        summary=body,
        first_kept_index=cut,
        tokens_before=estimate_tokens(messages),
        tokens_after=estimate_tokens([stand_in, *kept]),
        fallback=fallback,
    )
