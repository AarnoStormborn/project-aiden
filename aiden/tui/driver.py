"""The inline driver: event stream to terminal, with a live tail.

Inline-first per research/06 §Decision: finalized content goes into the **real scrollback**, so
native selection, tmux copy and Cmd-F keep working, and only the mutable tail lives in a
repainted region.

The invariant this module maintains:

    cells[0 : _committed_cells]   are on screen in real scrollback, written exactly once
    the open AssistantText cell   has its first `_assistant_lines_written` lines in scrollback
    everything else               is the live region, repainted each frame

Two rules make it work, and both were bugs first:

- A cell with status ``RUNNING`` is never committed. Committing a tool cell when it starts means
  its output and final status can never be shown.
- Assistant text is committed *incrementally* (stable lines only, per the chunker) and the rest
  stays live. Committing the whole cell when it closes would duplicate what was already written.

Input is not handled here beyond interruption: this milestone streams a question asked on the
command line. The keymap, editor and overlays attach at the same seam later.
"""

from __future__ import annotations

import shutil
import sys
import time
from dataclasses import dataclass
from math import ceil
from typing import IO

from ..events import (
    Event,
    RunFinished,
    TextDelta,
    ThinkingDelta,
)
from . import render
from .model import ABORTED, RUNNING, AssistantText, Transcript
from .stream import StreamController
from .theme import Theme
from .writer import LiveRegion, commit

DEFAULT_WIDTH_FALLBACK = 100


def terminal_width(stream: IO[str] | None = None) -> int:
    try:
        size = shutil.get_terminal_size(fallback=(DEFAULT_WIDTH_FALLBACK, 24))
    except OSError:  # pragma: no cover - unusual stdio
        return DEFAULT_WIDTH_FALLBACK
    return max(20, size.columns)


class TUIDriver:
    """Renders a harness run inline, streaming into scrollback.

    Implements the ``EventSink`` protocol, so ``run_loop`` never learns about the terminal.
    """

    def __init__(
        self,
        *,
        out: IO[str] | None = None,
        theme: Theme | None = None,
        width: int | None = None,
        show_thinking: bool = False,
    ) -> None:
        self.out = out or sys.stdout
        self.theme = theme or Theme.from_env()
        self.width = width or terminal_width(self.out)
        self.show_thinking = show_thinking

        self.transcript = Transcript()
        self.stream = StreamController()
        self.region = LiveRegion(width=self.width, sync=True)
        self.aborted = False

        self._committed_cells = 0
        # cell id -> lines of that assistant cell already written to scrollback
        self._assistant_lines: dict[int, int] = {}
        self._frames = 0
        self._bytes = 0
        #: Seconds spent producing each frame. The spec budgets frame *time* (p50 ≤ 3 ms,
        #: p99 ≤ 8 ms steady state), which cannot be checked by counting output bytes — a frame
        #: can be small and still slow if rendering is wasteful.
        self._frame_seconds: list[float] = []

    # ------------------------------------------------------------------ sink

    def emit(self, event: Event) -> None:
        if isinstance(event, TextDelta):
            self.transcript.apply(event)
            self.stream.push(event.text)
            self._tick()
            return

        if isinstance(event, ThinkingDelta):
            self.transcript.apply(event)
            self._tick()
            return

        was_open = self.transcript.tail() is not None
        self.transcript.apply(event)
        # A tool call or turn boundary closes the prose, so the rest of that cell is final.
        if was_open and self.transcript.tail() is None:
            self.stream.take_rest()
        self._tick()

        if isinstance(event, RunFinished):
            self.finish()

    def abort(self) -> None:
        """Mark the run aborted and keep whatever was produced.

        research/06 §What great agent UIs do #10: an interrupt preserves work and never erases.
        The open cell is marked ABORTED rather than left RUNNING, so it counts as final and its
        partial text plus the "aborted" marker are committed to scrollback instead of vanishing
        with the live region.
        """
        self.aborted = True
        # Mark *every* in-flight cell, not just the open assistant one. Interrupting during a
        # tool call used to leave a RUNNING cell in the live region, which the next erase
        # discarded — the spec says commit partials and mark them aborted, never erase.
        for cell in self.transcript.cells:
            if cell.status == RUNNING:
                cell.status = ABORTED
        self.stream.take_rest()
        self._tick()

    def finish(self) -> None:
        self.stream.take_rest()
        self.transcript.finalize()
        self._tick()
        self._emit(self.region.plan([]))

    # ------------------------------------------------------------------ rendering

    def _tick(self) -> None:
        """Produce one frame and record how long it took.

        The timer covers everything attributable to the frame: commit decisions, cell rendering
        and the write plan. It excludes the actual terminal write, because that is I/O the
        process does not control and would make the budget depend on the user's tty.
        """
        started = time.perf_counter()
        self._commit_ready()
        payload = self.region.plan(self._live_lines())
        self._frame_seconds.append(time.perf_counter() - started)
        self._emit(payload)

    def _commit_ready(self) -> None:
        """Write everything that is final into real scrollback, exactly once."""
        pending: list[str] = []

        open_cell = self.transcript.tail()
        if open_cell is not None:
            released = self.stream.take_commit()
            if released:
                lines = render.plain_lines(released, self.width, self.theme)
                self._assistant_lines[open_cell.id] = self._assistant_lines.get(
                    open_cell.id, 0
                ) + len(lines)
                pending.extend(lines)

        final_count = self._final_cell_count()
        for cell in self.transcript.cells[self._committed_cells : final_count]:
            pending.extend(self._final_lines(cell))
        self._committed_cells = final_count

        if not pending:
            return
        # Clear the live region first: committed lines push the screen, and an unerased tail
        # would be left stranded above the new text.
        self._emit(self.region.plan([]))
        self._emit(commit(pending))
        self.region.forget()

    def _final_cell_count(self) -> int:
        """Cells from the start that are final and therefore safe to commit."""
        count = 0
        open_cell = self.transcript.tail()
        for cell in self.transcript.cells:
            if cell is open_cell or cell.status == RUNNING:
                break
            count += 1
        return count

    def _final_lines(self, cell) -> list[str]:
        """Lines for a now-final cell, skipping assistant text already written out."""
        if isinstance(cell, AssistantText):
            written = self._assistant_lines.get(cell.id, 0)
            lines = render.plain_lines(cell.text, self.width, self.theme)
            self._assistant_lines[cell.id] = len(lines)
            return lines[written:]
        return render.render_cell(cell, self.width, self.theme).lines

    def _live_lines(self) -> list[str]:
        """The mutable region: uncommitted cells plus the streaming remainder."""
        lines: list[str] = []
        open_cell = self.transcript.tail()

        for cell in self.transcript.cells[self._committed_cells :]:
            if cell is open_cell:
                continue
            rendered = render.render_cell(cell, self.width, self.theme).lines
            if rendered:
                if lines:
                    lines.append("")
                lines.extend(rendered)

        if open_cell is not None:
            written = self._assistant_lines.get(open_cell.id, 0)
            all_lines = render.plain_lines(open_cell.text, self.width, self.theme)
            remainder = all_lines[written:]
            if remainder:
                if lines:
                    lines.append("")
                lines.extend(remainder)

        return lines

    def _emit(self, payload: str) -> None:
        if not payload:
            return
        self._frames += 1
        self._bytes += len(payload)
        self.out.write(payload)
        self.out.flush()

    # ------------------------------------------------------------------ lifecycle

    def resize(self, width: int) -> None:
        """A width change invalidates every wrapped line: full repaint is the sanctioned cost."""
        if width == self.width:
            return
        self.width = width
        self.region.resize(width)
        self.region.forget()
        # Committed scrollback cannot be re-wrapped; only the live region is repainted.
        self._tick()

    @property
    def stats(self) -> dict[str, int]:
        """Frame/byte counters, so the perf budget is measurable in tests."""
        return {"frames": self._frames, "bytes": self._bytes, "width": self.width}

    def frame_latency(self) -> FrameLatency:
        """Frame-time distribution, for the perf budget (spec §Perf budget)."""
        return FrameLatency.from_seconds(self._frame_seconds)


#: No-op context manager so call sites do not branch on whether the TUI is active.
#:
#: This deliberately does **not** install a SIGINT handler. An earlier version raised
#: KeyboardInterrupt from the handler, which unwinds through asyncio's selector: the exception
#: escapes from ``run_until_complete`` rather than from the awaiting coroutine, so the caller's
#: ``except KeyboardInterrupt`` never sees it and Python prints a traceback at the user. Letting
#: the default handler raise, and catching it at the top level, is both simpler and correct.
class InterruptGuard:
    def __init__(self, driver: TUIDriver) -> None:
        self.driver = driver

    def __enter__(self) -> InterruptGuard:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@dataclass(slots=True)
class FrameLatency:
    """Frame-time distribution in milliseconds.

    ``p50``/``p99`` use nearest-rank on the sorted samples, which is honest for the small sample
    counts a short run produces (a percentile estimate from 12 frames is a hint, not a
    measurement, and the spec's own budget work assumes a replay harness with many frames).
    """

    count: int = 0
    p50_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0
    total_ms: float = 0.0

    @classmethod
    def from_seconds(cls, samples: list[float]) -> FrameLatency:
        if not samples:
            return cls()
        ordered = sorted(samples)
        return cls(
            count=len(ordered),
            p50_ms=_percentile(ordered, 0.50) * 1000,
            p99_ms=_percentile(ordered, 0.99) * 1000,
            max_ms=ordered[-1] * 1000,
            total_ms=sum(ordered) * 1000,
        )

    def meets(self, *, p50_ms: float, p99_ms: float) -> bool:
        """Whether the run stayed inside the spec's steady-state budget."""
        return self.p50_ms <= p50_ms and self.p99_ms <= p99_ms

    def render(self) -> str:
        if not self.count:
            return "no frames"
        return (
            f"{self.count} frames · p50 {self.p50_ms:.2f}ms · p99 {self.p99_ms:.2f}ms · "
            f"max {self.max_ms:.2f}ms"
        )


def _percentile(ordered: list[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted list."""
    index = min(len(ordered) - 1, max(0, ceil(fraction * len(ordered)) - 1))
    return ordered[index]
