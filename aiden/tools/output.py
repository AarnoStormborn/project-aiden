"""Output shaping: cap, spill, and always leave a continuation hint.

research/02 §3 measured the problem this solves: one context-free ``rg`` over a 104 MB corpus
returned ~90k tokens in a single call, i.e. half a 200k window. The fix is not "ask nicely" —
it is a hard budget plus a pointer to the rest.

Spill-to-file follows pi: past the budget, the payload goes to disk and the model receives
the head plus the path ([r02] §3, "spill-to-file with the path in the message").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import config


@dataclass(slots=True)
class Capped:
    text: str
    truncated: bool
    hint: str
    total_bytes: int
    total_lines: int


def cap_text(
    text: str,
    *,
    max_bytes: int = config.READ_MAX_BYTES,
    max_lines: int = config.READ_MAX_LINES,
    offset: int = 0,
) -> Capped:
    """Trim ``text`` to the budget, computing the continuation hint for what was cut.

    ``offset`` is the line offset this chunk started at, so the hint points at the next
    unread line in the original file rather than in the chunk.
    """
    raw = text.encode("utf-8")
    total_bytes = len(raw)
    lines = text.splitlines()
    total_lines = len(lines)

    cut_reason = ""
    if total_bytes > max_bytes:
        cut_reason = f"{total_bytes} bytes exceeds the {max_bytes}-byte budget"
    elif total_lines > max_lines:
        cut_reason = f"{total_lines} lines exceeds the {max_lines}-line budget"

    if not cut_reason:
        return Capped(text, False, "", total_bytes, total_lines)

    # Cut on a line boundary so the model never sees half a line of code.
    if total_lines > max_lines:
        kept_lines = lines[:max_lines]
    else:
        kept_lines = []
        used = 0
        for line in lines:
            size = len(line.encode("utf-8")) + 1
            if used + size > max_bytes:
                break
            kept_lines.append(line)
            used += size

    if not kept_lines:
        # Nothing fitted. Returning the whole first line would blow the budget — a single very long
        # line (minified JS, a one-line JSON blob) is exactly when the cap matters most — so cut it
        # to the budget and mark the cut.
        first = lines[0] if lines else ""
        kept_lines = [first.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")]
    next_offset = offset + len(kept_lines)
    hint = (
        f"[truncated: {cut_reason}. Showing lines {offset + 1}-{next_offset} of "
        f"{total_lines}. Call read again with offset={next_offset} for the next chunk.]"
    )
    return Capped("\n".join(kept_lines), True, hint, total_bytes, total_lines)


def spill(text: str, *, name: str = "output", directory: Path | None = None) -> Path:
    """Write oversized output to the spill directory and return its path."""
    target_dir = directory or config.SPILL_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{config.new_session_id()}-{name}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def cap_with_spill(
    text: str,
    *,
    name: str,
    directory: Path | None = None,
    max_bytes: int = config.READ_MAX_BYTES,
    max_lines: int = config.READ_MAX_LINES,
) -> Capped:
    """Cap ``text``; if it is very large, spill the whole thing and point at the file."""
    capped = cap_text(text, max_bytes=max_bytes, max_lines=max_lines)
    if not capped.truncated:
        return capped

    # Only spill when the full payload is genuinely large: a small overage is cheaper to
    # re-request than to manage as a file.
    if capped.total_bytes > max_bytes * 4:
        path = spill(text, name=name, directory=directory)
        capped.hint = (
            f"[truncated: {capped.hint.strip('[]')} The full output was written to {path}.]"
        )
    return capped
