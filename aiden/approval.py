"""Approval: the last gate before a change reaches the working tree.

`research/01` §permissions puts approval after policy, and `architecture` §4.5 is blunt about why it
cannot be a suggestion: "the Refiner cannot write code" is a rule the **tool layer** enforces, not
something a prompt asks for politely.

Three properties this module is built around:

- **Deny by default when nobody can be asked.** A piped or scripted run has no one to prompt, so the
  honest answer is no. `--allow-writes` (or ``AIDEN_ALLOW_WRITES=1``) opts in, loudly.
- **A rejection carries the reason.** The model gets an error result containing the user's words, so
  it can adapt instead of retrying the same change.
- **Every decision is logged**, including who made it, because the transcript is the audit trail.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approved: bool
    decided_by: str = "user"
    note: str = ""

    @classmethod
    def yes(cls, decided_by: str = "user", note: str = "") -> ApprovalDecision:
        return cls(True, decided_by, note)

    @classmethod
    def no(cls, note: str, decided_by: str = "user") -> ApprovalDecision:
        return cls(False, decided_by, note)


class Approver(Protocol):
    """Something that can decide whether a change may be applied."""

    #: Whether a human can actually be asked. Drives the deny-by-default rule.
    interactive: bool

    async def request(
        self, *, name: str, path: str, diff: str, sensitive: bool, reason: str
    ) -> ApprovalDecision: ...


class DenyAll:
    """Refuses every write, with an explanation the model can act on."""

    interactive = False

    async def request(
        self,
        *,
        name: str = "",
        path: str = "",
        diff: str = "",
        sensitive: bool = False,
        reason: str = "",
    ) -> ApprovalDecision:
        return ApprovalDecision.no(
            "this run cannot ask for approval (no interactive terminal), so the change was not "
            "applied. Describe the change you want and the user can apply it, or re-run with "
            "--allow-writes.",
            decided_by="policy",
        )


class AllowAll:
    """Applies every write without asking.

    Only reachable through an explicit opt-in. ``decided_by`` says so, because a transcript that
    simply shows "approved" would misrepresent who approved it.
    """

    interactive = False

    def __init__(self, decided_by: str = "flag", note: str = "") -> None:
        self.decided_by = decided_by
        self.note = note or "writes were auto-approved by --allow-writes"

    async def request(
        self,
        *,
        name: str = "",
        path: str = "",
        diff: str = "",
        sensitive: bool = False,
        reason: str = "",
    ) -> ApprovalDecision:
        return ApprovalDecision.yes(decided_by=self.decided_by, note=self.note)


def default_approver(
    *,
    interactive: bool,
    allow_writes: bool = False,
    env: Mapping[str, str] | None = None,
    prompter: Approver | None = None,
) -> Approver:
    """Choose an approver for the current situation.

    Precedence: an explicit prompter, then the opt-in flag, then interactive prompting, then denial.
    """
    resolved_env: Mapping[str, str] = os.environ if env is None else env
    opted_in = allow_writes or (resolved_env.get("AIDEN_ALLOW_WRITES") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if prompter is not None:
        return prompter
    if opted_in:
        return AllowAll()
    if interactive:
        raise ValueError("an interactive approver must be supplied explicitly")
    return DenyAll()
