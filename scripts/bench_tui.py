#!/usr/bin/env python3
"""Frame-time benchmark for the TUI, replaying recorded events.

Closes the gap flagged in docs/plan/tui-milestone.md: the earlier numbers were output *size*
only, because frame *time* needs either a terminal or a replay. This is the replay.

Deterministic and CI-safe: it drives `TUIDriver` with a synthetic event stream that mirrors a
real run (streaming prose, tool calls, diagnostics, summary) into a null sink, and reports the
distribution against the spec's budget (research/06 §Perf budget):

    steady-state frame   p50 ≤ 3 ms, p99 ≤ 8 ms, ≤ 400 bytes
    full frame at 120x40 ≤ 12 ms
    output               ≤ 2 KB/frame during a normal stream

    uv run python scripts/bench_tui.py
    uv run python scripts/bench_tui.py --width 120 --tokens 4000 --assert-budget
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiden.events import (
    Diagnostic,
    RunFinished,
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from aiden.providers.types import Usage
from aiden.tui.driver import FrameLatency, TUIDriver
from aiden.tui.theme import Theme

BUDGET = {"p50_ms": 3.0, "p99_ms": 8.0, "bytes_per_frame": 2_000, "steady_bytes": 400}


class _NullWriter(io.StringIO):
    """Counts bytes without keeping them, so memory does not skew the measurement."""

    def write(self, s: str) -> int:
        self.written = getattr(self, "written", 0) + len(s)
        return len(s)


def synthetic_run(tokens: int, *, tool_lines: int = 12) -> list:
    """A run shaped like a real one: streaming prose, tool latency, a warning, a summary."""
    events: list = [
        RunStarted(
            session_id="bench",
            model="bench/model",
            question="Benchmark the renderer with a realistic stream.",
            cwd="/repo/project-aiden",
        ),
        ThinkingDelta(text="Considering the shape of a realistic stream. " * 4),
    ]

    # Prose arrives as words, like a streamed completion.
    words = ("the quick brown fox jumps over the lazy dog " * (tokens // 9 + 1)).split()
    for index in range(tokens):
        word = words[index % len(words)]
        events.append(TextDelta(text=word + ("\n" if index % 12 == 11 else " ")))

        # Every so often, a tool call with output, which is the largest render.
        if index % 40 == 39:
            call_id = f"c{index}"
            events.append(
                ToolCallStarted(call_id=call_id, name="read", arguments={"path": "aiden/loop.py"})
            )
            events.append(
                ToolCallFinished(
                    call_id=call_id,
                    name="read",
                    is_error=False,
                    duration_ms=2,
                    output_chars=tool_lines * 40,
                    output="\n".join(f"{i}\tline of file content" for i in range(tool_lines)),
                )
            )
            events.append(Diagnostic(message="a cap left a trace", level="warning"))

    events.append(
        RunFinished(
            stop_reason="end_turn",
            turns=3,
            tool_calls=tokens // 40,
            usage=Usage(input_tokens=1_000, output_tokens=tokens, cache_read_tokens=500),
            cost_usd=0.0012,
            session_path="/home/u/.aiden/sessions/--repo--/bench.jsonl",
            answer="done",
        )
    )
    return events


def run_benchmark(*, width: int, tokens: int, colour: bool) -> dict:
    out = _NullWriter()
    driver = TUIDriver(
        out=out, theme=Theme(dark=True, colour=colour, ascii_mode=not colour), width=width
    )

    wall_start = time.perf_counter()
    for event in synthetic_run(tokens):
        driver.emit(event)
    wall = time.perf_counter() - wall_start

    latency = driver.frame_latency()
    frames = max(1, driver.stats["frames"])
    return {
        "width": width,
        "tokens": tokens,
        "frames": driver.stats["frames"],
        "bytes": driver.stats["bytes"],
        "bytes_per_frame": driver.stats["bytes"] / frames,
        "wall_ms": wall * 1000,
        "latency": latency,
        "committed_cells": driver._committed_cells,
    }


def steady_state(width: int, iterations: int) -> dict:
    """The budget's steady-state case: ≤ 3 ms and ≤ 400 bytes per frame.

    Two distinct situations, both measured per frame rather than in total — an earlier version of
    this benchmark summed 200 frames and compared the sum against a per-frame budget, which is
    not the same statement at all.

    ``unchanged`` must be exactly zero bytes: nothing moved, so nothing may be written.
    ``spinner`` rotates one line, which is the realistic idle cost.
    """
    out = _NullWriter()
    driver = TUIDriver(out=out, theme=Theme(dark=True, colour=True), width=width)
    driver.emit(RunStarted(session_id="s", model="m", question="q", cwd="/repo"))
    driver.emit(ToolCallStarted(call_id="c1", name="read", arguments={"path": "a.py"}))

    # 1) Nothing changes: any byte written here is pure waste.
    before = out.written
    for _ in range(iterations):
        driver._tick()
    unchanged_bytes = out.written - before

    # 2) One live line genuinely changes per tick, the way a spinner does. This has to go
    #    through `emit` so the open cell actually grows: pushing straight into the stream buffer
    #    rendered nothing, and the first version of this benchmark reported a meaningless
    #    "0 bytes/frame" for it.
    before = out.written
    frames_before = driver.stats["frames"]
    for _ in range(iterations):
        driver.emit(TextDelta(text="."))
    spinner_bytes = out.written - before
    spinner_frames = max(1, driver.stats["frames"] - frames_before)

    return {
        "iterations": iterations,
        "unchanged_bytes": unchanged_bytes,
        "unchanged_per_frame": unchanged_bytes / iterations,
        "spinner_bytes_per_frame": spinner_bytes / spinner_frames,
        "latency": driver.frame_latency(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=120)
    ap.add_argument("--tokens", type=int, default=2_000)
    ap.add_argument("--colour", action="store_true", help="benchmark with SGR styling on")
    ap.add_argument("--assert-budget", action="store_true", help="exit 1 if the budget is missed")
    args = ap.parse_args()

    result = run_benchmark(width=args.width, tokens=args.tokens, colour=args.colour)
    latency: FrameLatency = result["latency"]

    print(
        f"replay: {result['tokens']} tokens @ {result['width']} cols "
        f"(colour={'on' if args.colour else 'off'})"
    )
    print(f"  frames            {result['frames']}")
    print(f"  total bytes       {result['bytes']:,}")
    print(
        f"  bytes/frame       {result['bytes_per_frame']:.0f}   (budget ≤ {BUDGET['bytes_per_frame']:,})"
    )
    print(f"  frame time        {latency.render()}")
    print(f"  wall clock        {result['wall_ms']:.1f}ms for the whole run")
    print(f"  budget p50/p99    ≤ {BUDGET['p50_ms']}ms / ≤ {BUDGET['p99_ms']}ms")

    steady = steady_state(args.width, 200)
    print(
        f"  unchanged frames  {steady['iterations']} ticks, "
        f"{steady['unchanged_bytes']} bytes total (must be 0)"
    )
    print(
        f"  spinner frame     {steady['spinner_bytes_per_frame']:.0f} bytes/frame "
        f"(budget ≤ {BUDGET['steady_bytes']})"
    )
    print(f"  frame time        {steady['latency'].render()}")

    failures: list[str] = []
    if latency.p99_ms > BUDGET["p99_ms"]:
        failures.append(f"p99 {latency.p99_ms:.2f}ms > {BUDGET['p99_ms']}ms")
    if latency.p50_ms > BUDGET["p50_ms"]:
        failures.append(f"p50 {latency.p50_ms:.2f}ms > {BUDGET['p50_ms']}ms")
    if result["bytes_per_frame"] > BUDGET["bytes_per_frame"]:
        failures.append(
            f"{result['bytes_per_frame']:.0f} bytes/frame > {BUDGET['bytes_per_frame']}"
        )
    if steady["unchanged_bytes"] != 0:
        failures.append(f"an unchanged frame wrote {steady['unchanged_bytes']} bytes; must be 0")
    if steady["spinner_bytes_per_frame"] > BUDGET["steady_bytes"]:
        failures.append(
            f"spinner frame {steady['spinner_bytes_per_frame']:.0f} bytes "
            f"> {BUDGET['steady_bytes']}"
        )

    if failures:
        print("\nBUDGET MISSED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1 if args.assert_budget else 0

    print("\nbudget met")
    return 0


if __name__ == "__main__":
    sys.exit(main())
