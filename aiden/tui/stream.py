"""Streaming: stable region vs mutable tail, and the commit cadence.

This module is the answer to the research's central claim about agent TUIs — *the hard problems
are streaming, not pixels* (research/06 §TL;DR). Three mechanisms, all copied from Codex and
Pi because both independently converged on them:

1. **Two regions.** Each stream splits into a *stable* region (safe to commit into real
   scrollback) and a *tail* (mutable, repainted in the live region).
2. **Commit at newline boundaries only.** ``markdown_stream.rs`` keeps the trailing incomplete
   line in the buffer; committing mid-line makes every keystroke of the model repaint a line
   the user is already reading.
3. **Hold back anything non-incremental.** A new table row can change every column's width, so
   tables and unterminated code fences stay in the tail and are rendered once, whole, when they
   close.

Plus the commit cadence: one line per tick normally, draining under pressure, with hysteresis
so the gears do not flap (``chunking.rs``'s ``EXIT_HOLD``/``REENTER_CATCH_UP_HOLD``).

Everything here is pure: text in, offsets out. No terminal, no clock, no I/O — so the golden
tests can assert on the split exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

FENCE = "```"
TABLE_PREFIX = "|"


def _line_offsets(lines: list[str]) -> list[int]:
    out: list[int] = []
    pos = 0
    for line in lines:
        out.append(pos)
        pos += len(line)
    return out


def holdback_start(committed: str) -> int | None:
    """Offset within ``committed`` from which output must be held back as tail, if any.

    Returns ``None`` when the whole string is safe to commit.
    """
    if not committed:
        return None

    lines = committed.splitlines(keepends=True)
    offsets = _line_offsets(lines)

    # An unterminated fence swallows everything after it: hold from the opening fence so the
    # code block is syntax-highlighted once, not repeatedly as it grows.
    open_fence_at: int | None = None
    in_fence = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(FENCE):
            if not in_fence:
                in_fence = True
                open_fence_at = index
            else:
                in_fence = False
                open_fence_at = None
    if in_fence and open_fence_at is not None:
        return offsets[open_fence_at]

    # A trailing run of table rows is still being shaped: adding a row can reflow every column.
    last_content = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip():
            last_content = index
            break
    if last_content is not None and lines[last_content].lstrip().startswith(TABLE_PREFIX):
        start = last_content
        while start > 0 and lines[start - 1].lstrip().startswith(TABLE_PREFIX):
            start -= 1
        return offsets[start]

    return None


def split(text: str) -> tuple[str, str]:
    """Split accumulated stream text into ``(stable, tail)``.

    ``stable`` ends at a newline and excludes any structure still being built; ``tail`` is the
    incomplete last line plus that held-back structure.
    """
    if not text:
        return "", ""

    last_newline = text.rfind("\n")
    if last_newline == -1:
        return "", text

    candidate = text[: last_newline + 1]
    remainder = text[last_newline + 1 :]

    cut = holdback_start(candidate)
    if cut is not None:
        return candidate[:cut], candidate[cut:] + remainder
    return candidate, remainder


@dataclass
class Chunker:
    """How many pending lines to commit this tick, with hysteresis.

    ``Smooth`` releases one line per tick so the transcript reads like it is being typed;
    ``CatchUp`` drains everything when the model outruns the renderer. Exiting catch-up is held
    for ``EXIT_HOLD`` quiet ticks to avoid oscillating between gears.
    """

    smooth_batch: int = 1
    catch_up_threshold: int = 8
    exit_hold: int = 3

    gear: str = "smooth"
    _calm_ticks: int = 0

    def next_batch(self, pending: int) -> int:
        if pending <= 0:
            self._calm_ticks += 1
            if self.gear == "catch_up" and self._calm_ticks >= self.exit_hold:
                self.gear = "smooth"
            return 0

        if self.gear == "smooth" and pending > self.catch_up_threshold:
            self.gear = "catch_up"
            self._calm_ticks = 0
        elif self.gear == "catch_up":
            if pending <= self.smooth_batch:
                self._calm_ticks += 1
                if self._calm_ticks >= self.exit_hold:
                    self.gear = "smooth"
            else:
                self._calm_ticks = 0

        if self.gear == "catch_up":
            return pending
        return min(self.smooth_batch, pending)

    @property
    def catching_up(self) -> bool:
        return self.gear == "catch_up"


class StreamController:
    """Tracks one assistant stream and what may be committed from it."""

    def __init__(self, chunker: Chunker | None = None) -> None:
        self.chunker = chunker or Chunker()
        self.text = ""
        self.committed_chars = 0
        self._finalized = False

    def push(self, delta: str) -> None:
        self.text += delta

    def finalize(self) -> None:
        """Force everything committable. Called at turn end, when nothing more is coming."""
        self._finalized = True
        self.committed_chars = len(self.text)

    @property
    def _uncommitted(self) -> str:
        return self.text[self.committed_chars :]

    @property
    def stable(self) -> str:
        if self._finalized:
            return self.text
        return self.text[: self.committed_chars] + split(self._uncommitted)[0]

    @property
    def tail(self) -> str:
        """The mutable remainder: only what has *not* already been committed."""
        if self._finalized:
            return ""
        return split(self._uncommitted)[1]

    def pending_lines(self) -> list[str]:
        """Stable lines not yet committed."""
        stable_rest = split(self._uncommitted)[0]
        return [line for line in stable_rest.splitlines(keepends=True) if line.endswith("\n")]

    def take_commit(self) -> str:
        """Consume as many stable lines as the chunker allows, returning the text to commit."""
        lines = self.pending_lines()
        if not lines:
            return ""
        batch = self.chunker.next_batch(len(lines))
        if batch == 0:
            return ""
        take = "".join(lines[:batch])
        self.committed_chars += len(take)
        return take

    def take_rest(self) -> str:
        """Take everything still buffered, *including* the partial line.

        Used when a text segment ends but more may follow (a tool call interrupts the prose, or
        the turn ends and a later turn adds more). This must **not** finalize the stream: doing
        so would make ``tail`` permanently empty and the live region would never paint again.
        """
        rest = self.text[self.committed_chars :]
        self.committed_chars = len(self.text)
        return rest

    def reset(self) -> None:
        """Start a new text segment (after a tool call, or a new turn)."""
        self.text = ""
        self.committed_chars = 0
        self._finalized = False
        self.chunker = Chunker(
            smooth_batch=self.chunker.smooth_batch,
            catch_up_threshold=self.chunker.catch_up_threshold,
            exit_hold=self.chunker.exit_hold,
        )

    def flush(self) -> str:
        """Commit everything and mark the stream closed (end of run or abort)."""
        rest = self.take_rest()
        self._finalized = True
        return rest
