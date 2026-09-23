"""The inline driver: commit ordering, flicker rules, and the perf budget.

Driven through a StringIO so the assertions are on real output bytes.
"""

from __future__ import annotations

import io

import pytest

from aiden.events import (
    Diagnostic,
    RunFinished,
    RunStarted,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from aiden.providers.types import Usage
from aiden.tui.driver import TUIDriver
from aiden.tui.theme import Theme
from aiden.tui.writer import SYNC_START


@pytest.fixture
def out() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def driver(out: io.StringIO, theme: Theme) -> TUIDriver:
    return TUIDriver(out=out, theme=theme, width=80)


def test_streamed_text_appears_in_the_output(driver: TUIDriver, out: io.StringIO):
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="hello "))
    driver.emit(TextDelta(text="world"))
    assert "hello" in out.getvalue()
    assert "world" in out.getvalue()


def test_no_screen_clear_is_ever_emitted(driver: TUIDriver, out: io.StringIO):
    """Flicker rule 2: never clear; that would destroy the user's scrollback."""
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="some streamed text\nmore\n"))
    driver.finish()
    assert "\x1b[2J" not in out.getvalue()


def test_frames_use_synchronized_output(driver: TUIDriver, out: io.StringIO):
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="x"))
    assert SYNC_START in out.getvalue()


def test_identical_tail_does_not_repaint(driver: TUIDriver, out: io.StringIO):
    """Steady-state budget: a repeated frame must cost nothing."""
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="stable"))
    before = len(out.getvalue())
    driver.emit(Diagnostic(message="no change to the tail"))  # non-text event, same tail
    # The tail is unchanged, so no additional tail repaint may be emitted.
    assert len(out.getvalue()) - before < 400


def test_tool_calls_are_committed_to_scrollback(driver: TUIDriver, out: io.StringIO):
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(ToolCallStarted(call_id="c1", name="read", arguments={"path": "a.py"}))
    driver.emit(
        ToolCallFinished(
            call_id="c1",
            name="read",
            is_error=False,
            duration_ms=2,
            output_chars=10,
            output="contents",
        )
    )
    driver.finish()
    body = out.getvalue()
    assert "read(" in body
    assert "contents" in body


def test_streamed_text_is_committed_once_then_not_reprinted(driver: TUIDriver, out: io.StringIO):
    """Flicker rule 3: never re-print finalized lines."""
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="committed line\n"))
    driver.emit(ToolCallStarted(call_id="c1", name="glob", arguments={}))
    committed_so_far = out.getvalue().count("committed line")
    driver.emit(TextDelta(text="later text\n"))
    driver.finish()
    # The phrase may appear again in a repaint of the *live* region, but the finalized copy is
    # written once; the total should stay small rather than growing with every frame.
    assert out.getvalue().count("committed line") <= committed_so_far + 2


def test_abort_preserves_work_and_marks_the_cell(driver: TUIDriver, out: io.StringIO):
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="partial answer"))
    driver.abort()
    assert driver.aborted
    assert "partial answer" in out.getvalue()
    assert driver.transcript.tail() is None or driver.transcript.tail().status == "aborted"


def test_finish_prints_the_run_summary(driver: TUIDriver, out: io.StringIO):
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="done\n"))
    driver.emit(
        RunFinished(
            stop_reason="end_turn",
            turns=2,
            tool_calls=3,
            usage=Usage(input_tokens=100, output_tokens=20),
            cost_usd=0.0012,
            session_path="/sessions/x.jsonl",
        )
    )
    body = out.getvalue()
    assert "turns 2" in body
    assert "$0.0012" in body


def test_resize_forces_a_full_repaint(driver: TUIDriver, out: io.StringIO):
    """A width change must repaint the live region (committed scrollback cannot be re-wrapped)."""
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="an uncommitted partial line"))
    before = len(out.getvalue())

    driver.resize(120)

    assert driver.width == 120
    assert len(out.getvalue()) > before
    assert "an uncommitted partial line" in out.getvalue()[before:]


def test_resize_with_nothing_live_emits_nothing(driver: TUIDriver, out: io.StringIO):
    """No live content means nothing to re-wrap: emitting a frame would be pure waste."""
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="already committed\n"))
    before = len(out.getvalue())

    driver.resize(120)

    assert len(out.getvalue()) == before


def test_frame_budget_during_a_normal_stream(driver: TUIDriver):
    """Perf budget: output stays bounded per frame rather than growing with text length."""
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    for i in range(200):
        driver.emit(TextDelta(text=f"token{i} "))
    stats = driver.stats
    assert stats["frames"] > 0
    average = stats["bytes"] / max(1, stats["frames"])
    assert average < 2_000, f"average frame was {average:.0f} bytes"


# --------------------------------------------------------------- interruption (regression)


def test_abort_commits_partial_text_with_an_aborted_marker(driver: TUIDriver, out: io.StringIO):
    """An interrupt preserves work: partial text is committed, not discarded with the tail."""
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="half an answer"))
    driver.abort()

    body = out.getvalue()
    assert driver.aborted
    assert "half an answer" in body
    # The cell is final (not RUNNING), so its text is in scrollback rather than the live region.
    assert driver._committed_cells > 0


def test_abort_does_not_install_a_signal_handler(driver: TUIDriver):
    """Regression: raising from a SIGINT handler unwound through asyncio's selector.

    The exception escaped `run_until_complete` instead of the awaiting coroutine, so the CLI's
    `except KeyboardInterrupt` never ran and the user got a traceback. SIGINT is now left to the
    default handler and caught at the top level.
    """
    import signal

    from aiden.tui.driver import InterruptGuard

    before = signal.getsignal(signal.SIGINT)
    with InterruptGuard(driver):
        during = signal.getsignal(signal.SIGINT)
    after = signal.getsignal(signal.SIGINT)

    assert during is before, "the guard must not replace the SIGINT handler"
    assert after is before


def test_abort_during_a_tool_call_preserves_the_tool_cell(driver: TUIDriver, out: io.StringIO):
    """Interrupting mid-tool must not erase the cell: mark it aborted and commit it."""
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(ToolCallStarted(call_id="c1", name="grep", arguments={"pattern": "x"}))
    driver.abort()

    body = out.getvalue()
    assert "grep(" in body, "the in-flight tool cell was erased"
    assert "aborted" in body, "the cell must be marked, not silently dropped"
    assert all(c.status != "running" for c in driver.transcript.cells)
