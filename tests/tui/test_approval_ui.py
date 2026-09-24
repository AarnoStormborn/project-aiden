"""Approval in the UI: the diff is shown before the question, and only a yes applies.

research/06 §What great agent UIs do #9: a prompt that only says "Allow?" trains the user to say yes
to nothing. So the rendering tests assert on the diff being visible.
"""

from __future__ import annotations

import io

import pytest

from aiden.diffutil import render_lines, stats, unified
from aiden.events import ApprovalRequested, ApprovalResolved, RunStarted
from aiden.tui.approval_ui import PromptApprover
from aiden.tui.driver import TUIDriver
from aiden.tui.model import ERROR, OK, PENDING, Approval, Transcript
from aiden.tui.render import render_cell
from aiden.tui.theme import Theme


@pytest.fixture
def theme() -> Theme:
    return Theme(colour=False, ascii_mode=True)


# --------------------------------------------------------------------------- diffutil


def test_unified_diff_marks_additions_and_removals():
    diff = unified("a\nb\n", "a\nc\n", "f.py")
    assert "-b" in diff
    assert "+c" in diff


def test_identical_text_produces_no_diff():
    assert unified("same\n", "same\n", "f.py") == ""


def test_stats_counts_lines_by_kind():
    result = stats("keep\nremove\n", "keep\nadd\n")
    assert result.added == 1
    assert result.removed == 1
    assert result.render() == "+1 -1"


def test_render_lines_classifies_for_the_theme_tokens():
    kinds = {kind for kind, _line in render_lines(unified("a\n", "b\n", "f.py"))}
    assert "add" in kinds and "del" in kinds


def test_render_lines_truncates_with_a_visible_note():
    big = "".join(f"line{i}\n" for i in range(100))
    lines = render_lines(unified("", big, "f.py"), max_lines=10)
    assert any("more diff lines" in line for _kind, line in lines)


# --------------------------------------------------------------------------- the cell


def _transcript() -> Transcript:
    t = Transcript()
    t.apply(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    t.apply(
        ApprovalRequested(
            call_id="c1", name="edit", path="src/app.py", diff=unified("x = 1\n", "x = 2\n", "a.py")
        )
    )
    return t


def test_an_approval_cell_starts_pending(theme: Theme):
    cell = _transcript().of(Approval)[0]
    assert cell.status == PENDING
    body = "\n".join(render_cell(cell, 80, theme).lines)
    assert "approve?" in body


def test_the_pending_cell_shows_the_change_not_just_the_path(theme: Theme):
    cell = _transcript().of(Approval)[0]
    body = "\n".join(render_cell(cell, 80, theme).lines)
    assert "- x = 1" in body
    assert "+ x = 2" in body


def test_resolving_the_approval_updates_the_cell(theme: Theme):
    t = _transcript()
    t.apply(ApprovalResolved(call_id="c1", approved=True, decided_by="user"))
    cell = t.of(Approval)[0]
    assert cell.status == OK
    body = "\n".join(render_cell(cell, 80, theme).lines)
    assert "applied" in body
    assert "x = 2" not in body, "the diff is no longer actionable once decided"


def test_a_declined_approval_says_so_and_who_decided(theme: Theme):
    t = _transcript()
    t.apply(ApprovalResolved(call_id="c1", approved=False, decided_by="flag", note="auto-approved"))
    cell = t.of(Approval)[0]
    assert cell.status == ERROR
    body = "\n".join(render_cell(cell, 80, theme).lines)
    assert "declined" in body
    assert "flag" in body


def test_a_sensitive_path_is_called_out(theme: Theme):
    t = Transcript()
    t.apply(
        ApprovalRequested(call_id="c1", name="write", path=".env", diff="+.env", sensitive=True)
    )
    body = "\n".join(render_cell(t.of(Approval)[0], 80, theme).lines)
    assert "sensitive" in body


# --------------------------------------------------------------------------- the approver


def make(driver, answers: list[str]) -> PromptApprover:
    out: list[str] = []
    remaining = list(answers)
    prompter = PromptApprover(
        driver=driver,
        read=lambda _p="": remaining.pop(0) if remaining else "",
        write=out.append,
    )
    prompter.written = out  # type: ignore[attr-defined]
    return prompter


async def test_yes_applies_and_is_recorded_as_the_users_decision(theme: Theme):
    driver = TUIDriver(out=io.StringIO(), theme=theme, width=80)
    prompter = make(driver, ["y"])
    decision = await prompter.request(
        name="edit", path="a.py", diff=unified("x\n", "y\n", "a.py"), sensitive=False, reason=""
    )
    assert decision.approved
    assert decision.decided_by == "user"


@pytest.mark.parametrize("answer", ["n", "no", "", "   ", "maybe"])
async def test_anything_that_is_not_an_explicit_yes_is_a_no(answer: str, theme: Theme):
    """The safe default for an unanswered or unclear write is refusal."""
    driver = TUIDriver(out=io.StringIO(), theme=theme, width=80)
    decision = await make(driver, [answer]).request(
        name="edit", path="a.py", diff="+x", sensitive=False, reason=""
    )
    assert not decision.approved, f"{answer!r} must not approve"


async def test_eof_is_a_no_not_a_hang(theme: Theme):
    driver = TUIDriver(out=io.StringIO(), theme=theme, width=80)

    def boom(_p: str = "") -> str:
        raise EOFError

    prompter = PromptApprover(driver=driver, read=boom, write=lambda _s: None)
    decision = await prompter.request(
        name="write", path="a.py", diff="+x", sensitive=False, reason=""
    )
    assert not decision.approved


async def test_the_user_sees_the_diff_before_being_asked(theme: Theme):
    driver = TUIDriver(out=io.StringIO(), theme=theme, width=80)
    prompter = make(driver, ["y"])
    await prompter.request(
        name="edit",
        path="src/app.py",
        diff=unified("return 1\n", "return 2\n", "src/app.py"),
        sensitive=False,
        reason="fix the return value",
    )
    shown = "\n".join(prompter.written)  # type: ignore[attr-defined]
    assert "src/app.py" in shown
    assert "- return 1" in shown
    assert "+ return 2" in shown
    assert "fix the return value" in shown


async def test_the_live_region_is_cleared_before_prompting(theme: Theme):
    """Otherwise the question is drawn over a frame the writer still owns."""
    from aiden.events import TextDelta

    driver = TUIDriver(out=io.StringIO(), theme=theme, width=80)
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="streaming"))
    assert driver.region.drawn_lines

    await make(driver, ["n"]).request(
        name="edit", path="a.py", diff="+x", sensitive=False, reason=""
    )
    assert driver.region.drawn_lines == []


async def test_sensitive_paths_can_be_configured_to_skip_the_prompt(theme: Theme):
    driver = TUIDriver(out=io.StringIO(), theme=theme, width=80)
    prompter = PromptApprover(
        driver=driver, read=lambda _p="": "", write=lambda _s: None, auto_approve_sensitive=True
    )
    decision = await prompter.request(
        name="write", path=".env", diff="+x", sensitive=True, reason=""
    )
    assert decision.approved
    assert decision.decided_by == "auto", "an auto-approval must not look like the user's"


def test_a_long_target_is_not_repeated_in_the_question():
    """The diff above already shows it; repeating a wrapped shell command makes the prompt unreadable."""
    from aiden.tui.approval_ui import _question

    assert _question("edit", "aiden/loop.py") == "edit aiden/loop.py? [y/N]"
    assert _question("bash", "rm -rf build") == "run rm -rf build? [y/N]"
    assert _question("web_fetch", "https://example.com/x") == "fetch https://example.com/x? [y/N]"

    long_command = 'find build -maxdepth 2; echo "--- tracked files ---"; git ls-files build'
    assert _question("bash", long_command) == "run this? [y/N]"
    assert _question("edit", "line one\nline two") == "edit this? [y/N]"


def test_an_empty_target_still_asks_a_readable_question():
    """Regression: the prompt read `apply this change to ?` when a tool named no path."""
    from aiden.tui.approval_ui import _question

    assert _question("web_fetch", "") == "fetch this? [y/N]"
    assert _question("edit", "") == "edit this? [y/N]"
