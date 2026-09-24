"""Interactive approval: show the diff, then ask.

The order is the point. `research/06` §What great agent UIs do #9: "A prompt that only says
'Allow?' trains the user to say yes to nothing." So the diff is rendered into the transcript *before*
the question is asked — the loop emits ``ApprovalRequested`` to the sink, the driver draws it, and
only then does this module prompt.

Anything that is not an explicit yes is a no. A stray newline, a Ctrl-C, or a closed stdin all mean
"do not change my files", because the safe default for an unanswered write is refusal.
"""

from __future__ import annotations

from collections.abc import Callable

from ..approval import ApprovalDecision
from ..diffutil import render_lines

YES = {"y", "yes", "ok", "apply"}
NO = {"n", "no", "reject", "cancel", ""}


class PromptApprover:
    """Asks at the terminal, one change at a time."""

    interactive = True

    def __init__(
        self,
        *,
        driver: object | None = None,
        read: Callable[[str], str] | None = None,
        write: Callable[[str], None] | None = None,
        auto_approve_sensitive: bool = False,
    ) -> None:
        self.driver = driver
        self._read = read
        self._write = write or print
        self.auto_approve_sensitive = auto_approve_sensitive
        #: Files approved this session, so a repeated edit to the same file is not re-asked for the
        #: same question — the user's answer already covered that path.
        self.approved_paths: set[str] = set()

    async def request(
        self,
        *,
        name: str,
        path: str,
        diff: str,
        sensitive: bool = False,
        reason: str = "",
    ) -> ApprovalDecision:
        if sensitive and self.auto_approve_sensitive:
            return ApprovalDecision.yes(
                decided_by="auto", note="sensitive paths auto-approved by configuration"
            )

        # The live region has to go before anything else can use the terminal, or the prompt is
        # drawn over the frame the renderer still believes is on screen.
        suspend = getattr(self.driver, "before_prompt", None)
        if callable(suspend):
            suspend()

        self._write("")
        self._write(_render_request(name, path, diff, sensitive=sensitive, reason=reason))

        answer = (await self._ask(f"{_question(name, path)} ")).strip().lower()
        if answer in YES:
            self.approved_paths.add(path)
            return ApprovalDecision.yes()
        if answer in NO:
            return ApprovalDecision.no("the user declined this change")
        # Unknown input is a refusal: guessing "yes" from a typo is the wrong direction to fail.
        return ApprovalDecision.no(f"the user answered {answer!r}, which is not a yes")

    async def _ask(self, prompt: str) -> str:
        if self._read is not None:
            try:
                return self._read(prompt)
            except (EOFError, KeyboardInterrupt):
                return ""
        return await _prompt_async(prompt)


#: Above this, the target is not repeated in the question. The diff above already shows it, and a
#: wrapped multi-line shell command makes the one line the user must read unreadable.
QUESTION_TARGET_LIMIT = 48


#: The verb per tool, so the question describes what is actually about to happen.
VERBS = {"web_fetch": "fetch", "bash": "run", "edit": "edit", "write": "write"}


def _question(name: str, target: str) -> str:
    """The question, without repeating a payload the diff has already shown in full."""
    verb = VERBS.get(name, "apply")
    if "\n" in target or len(target) > QUESTION_TARGET_LIMIT:
        return f"{verb} this? [y/N]"
    if not target:
        return f"{verb} this? [y/N]"
    return f"{verb} {target}? [y/N]"


def _render_request(
    name: str, path: str, diff: str, *, sensitive: bool, reason: str, max_lines: int = 40
) -> str:
    """The block shown before the question: what, where, and the actual change."""
    out: list[str] = []
    marker = "⚠ " if sensitive else ""
    out.append(f"{marker}{name} → {path}")
    if sensitive:
        out.append("  this is a sensitive path (secrets or configuration)")
    if reason:
        out.append(f"  reason given: {reason}")
    out.append("")
    shown = 0
    for kind, line in render_lines(diff, max_lines=max_lines):
        prefix = {"add": "+", "del": "-", "ctx": " ", "meta": " "}[kind]
        out.append(f"  {prefix} {line[1:] if kind in {'add', 'del'} else line}")
        shown += 1
    if shown >= max_lines:
        out.append("  … diff truncated for review; use `aiden sessions show --tools` for the rest")
    return "\n".join(out)


async def _prompt_async(prompt: str) -> str:
    """Read one line inside the running loop.

    ``PromptSession.prompt()`` is synchronous and calls ``asyncio.run`` internally, which cannot nest
    — the same trap that broke the TUI's first launch.
    """
    from prompt_toolkit import PromptSession

    session: PromptSession[str] = PromptSession()
    return await session.prompt_async(prompt, is_password=False)
