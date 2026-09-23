"""Perf budget, pinned in CI.

The specs live in research/06 §Perf budget. They were previously unverifiable in the test suite —
the milestone doc admitted the numbers were output-size only — so a regression in the render path
would have been invisible. These tests use the same replay shape as ``scripts/bench_tui.py``, but
small enough to run with the rest of the suite.

Measured headroom is large (frame time is ~160x under budget at the time of writing), so these
assert the spec values directly rather than a loosened CI variant: a change that pushes rendering
anywhere near 3 ms is a real regression, not noise.
"""

from __future__ import annotations

import io

import pytest
from scripts.bench_tui import synthetic_run

from aiden.events import RunFinished, RunStarted, TextDelta, ToolCallFinished, ToolCallStarted
from aiden.providers.types import Usage
from aiden.tui.driver import TUIDriver
from aiden.tui.theme import Theme

P50_BUDGET_MS = 3.0
P99_BUDGET_MS = 8.0
STEADY_BYTES = 400
STREAM_BYTES = 2_000


class NullWriter(io.StringIO):
    def write(self, s: str) -> int:
        self.written = getattr(self, "written", 0) + len(s)
        return len(s)


def test_streaming_frame_time_and_size_stay_inside_budget():
    out = NullWriter()
    driver = TUIDriver(out=out, theme=Theme(colour=False, ascii_mode=True), width=120)

    for event in synthetic_run(600):
        driver.emit(event)

    latency = driver.frame_latency()
    assert latency.count > 0
    assert latency.p50_ms <= P50_BUDGET_MS, latency.render()
    assert latency.p99_ms <= P99_BUDGET_MS, latency.render()

    per_frame = driver.stats["bytes"] / max(1, driver.stats["frames"])
    assert per_frame <= STREAM_BYTES, f"{per_frame:.0f} bytes/frame"


def test_an_unchanged_frame_writes_nothing():
    """The cheapest possible frame must be free; anything else is pure waste."""
    out = NullWriter()
    driver = TUIDriver(out=out, theme=Theme(colour=True), width=120)
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(ToolCallStarted(call_id="c1", name="read", arguments={"path": "a.py"}))

    before = out.written
    for _ in range(100):
        driver._tick()
    assert out.written - before == 0


def test_spinner_frame_stays_under_the_steady_state_budget():
    out = NullWriter()
    driver = TUIDriver(out=out, theme=Theme(colour=True), width=120)
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))

    before = out.written
    frames_before = driver.stats["frames"]
    for _ in range(100):
        driver.emit(TextDelta(text="."))
    frames = max(1, driver.stats["frames"] - frames_before)

    per_frame = (out.written - before) / frames
    assert per_frame <= STEADY_BYTES, f"{per_frame:.0f} bytes/frame"


def test_a_full_frame_at_120x40_is_inside_budget():
    """Spec: a full frame at 120x40 ≤ 12 ms. This is the widest single paint we do."""
    out = NullWriter()
    driver = TUIDriver(out=out, theme=Theme(colour=True), width=120)

    driver.emit(RunStarted(session_id="s", model="m", question="question " * 20, cwd="/repo"))
    for index in range(20):
        driver.emit(
            ToolCallStarted(call_id=f"c{index}", name="read", arguments={"path": f"f{index}.py"})
        )
        driver.emit(
            ToolCallFinished(
                call_id=f"c{index}",
                name="read",
                is_error=False,
                duration_ms=1,
                output_chars=400,
                output="\n".join(f"{i}\tcontent" for i in range(20)),
            )
        )
    driver.emit(TextDelta(text="final answer line\n" * 20))
    driver.emit(
        RunFinished(
            stop_reason="end_turn",
            turns=21,
            tool_calls=20,
            usage=Usage(input_tokens=100, output_tokens=100),
            cost_usd=0.001,
        )
    )

    latency = driver.frame_latency()
    assert latency.max_ms <= 12.0, latency.render()


def test_colour_does_not_materially_change_frame_cost():
    """Styling is a prefix per line; if it were re-parsed per frame this would diverge."""
    timings = {}
    for colour in (False, True):
        out = NullWriter()
        driver = TUIDriver(out=out, theme=Theme(colour=colour, ascii_mode=not colour), width=120)
        for event in synthetic_run(400):
            driver.emit(event)
        timings[colour] = driver.frame_latency().p99_ms

    # Generous factor: we care about a blow-up (e.g. re-parsing styles per frame), not jitter.
    assert timings[True] <= max(0.5, timings[False] * 5), timings


@pytest.mark.parametrize("width", [47, 80, 120, 200])
def test_budget_holds_at_every_tested_width(width: int):
    out = NullWriter()
    driver = TUIDriver(out=out, theme=Theme(colour=False, ascii_mode=True), width=width)
    for event in synthetic_run(300):
        driver.emit(event)
    assert driver.frame_latency().p99_ms <= P99_BUDGET_MS
