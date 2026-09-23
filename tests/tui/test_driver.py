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
    """No live content means nothing to re-wrap: emitting a frame would be pure waste.

    The run must be *settled* first. A line with no blank line after it is not a complete markdown
    block, so it stays in the live region until the segment closes — which is why this test ends
    the run before resizing.
    """
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="already committed\n"))
    driver.emit(
        RunFinished(stop_reason="end_turn", turns=1, tool_calls=0, usage=Usage(), cost_usd=0.0)
    )
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


# ----------------------------------------------------- markdown rendering (regression)


def _committed_output(driver_out: io.StringIO) -> str:
    """Only the payloads written to real scrollback.

    Live-region frames are wrapped in synchronized-output markers; committed lines are not. That
    distinction is what lets a test separate "what the user keeps" from "what was repainted".
    Negative matching is required rather than splitting on the start marker, because the first
    frame's start marker is consumed by the split and its body would otherwise look committed.
    """
    import re

    return re.sub(r"\x1b\[\?2026h.*?\x1b\[\?2026l", "", driver_out.getvalue(), flags=re.S)


def _run_markdown(out: io.StringIO, theme: Theme, *, settle: bool = True) -> TUIDriver:
    driver = TUIDriver(out=out, theme=theme, width=72)
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="## Heading\n\nSome **bold** and `code` here.\n\n"))
    driver.emit(TextDelta(text="| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nvalue = 1\n```\n"))
    if settle:
        driver.emit(
            RunFinished(stop_reason="end_turn", turns=1, tool_calls=0, usage=Usage(), cost_usd=0.0)
        )
    return driver


def test_committed_assistant_text_is_rendered_markdown(out: io.StringIO, theme: Theme):
    """Regression: `markdown_block` existed but was never called, so answers showed raw source.

    The spec's design is stream plain, render at block boundaries; before this the transcript
    showed `**bold**`, `|---|` tables and ``` fences verbatim.
    """
    _run_markdown(out, theme)
    committed = _committed_output(out)

    assert "Some **bold**" not in committed, "raw markdown emphasis reached scrollback"
    assert "| a | b |" not in committed, "raw markdown table reached scrollback"
    assert "```" not in committed, "raw code fence reached scrollback"
    # ...and the content itself survives.
    assert "bold" in committed
    assert "value = 1" in committed


def test_markdown_is_rendered_but_the_live_region_stays_plain(out: io.StringIO, theme: Theme):
    """Re-parsing markdown on every delta is the expensive path the spec warns about."""
    driver = TUIDriver(out=out, theme=theme, width=72)
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="Some **bold** text"))

    live = "".join(driver._live_lines())
    assert "**bold**" in live, "the in-flight tail is shown plainly, not re-parsed per delta"


def test_an_unclosed_block_is_not_committed_early(out: io.StringIO, theme: Theme):
    """A paragraph is only renderable once its block closes."""
    driver = TUIDriver(out=out, theme=theme, width=72)
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(TextDelta(text="an open paragraph\nstill open\n"))
    assert _committed_output(out).count("open paragraph") == 0

    driver.emit(TextDelta(text="\n"))  # blank line closes the block
    assert "open paragraph" in _committed_output(out)


def test_a_wall_of_text_stays_bounded_in_the_live_region(out: io.StringIO, theme: Theme):
    """No blank lines means no block boundary, so the cap must release lines."""
    from aiden.tui.stream import LIVE_LINE_CAP

    driver = TUIDriver(out=out, theme=theme, width=72)
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    for _ in range(LIVE_LINE_CAP * 3):
        driver.emit(TextDelta(text="a line with no blank after it\n"))

    assert len(driver._live_lines()) <= LIVE_LINE_CAP + 1
    assert "a line with no blank" in _committed_output(out)
