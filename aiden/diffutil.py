"""Diff computation, with no UI dependency.

Two consumers, which is why it lives outside ``aiden/tui``: the approval prompt needs to show what
a change will do *before* it is applied, and the TUI needs to render the same diff as a cell. Tools
must not import from the UI layer, so the computation sits here and the rendering sits there.

Line-level diffs are enough. `research/02` §4 chose exact-string replacement as the *edit* format
because generated unified diffs failed to apply on 51% of attempts — but that is about what the
model emits, not about how we show a change to a human, where a unified diff is exactly right.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DiffStats:
    added: int
    removed: int

    def render(self) -> str:
        return f"+{self.added} -{self.removed}"

    @property
    def empty(self) -> bool:
        return self.added == 0 and self.removed == 0


def unified(old: str, new: str, path: str, *, context: int = 3) -> str:
    """A unified diff of ``old`` to ``new``, or ``""`` when they are identical."""
    if old == new:
        return ""
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
    )
    return "".join(lines)


def stats(old: str, new: str) -> DiffStats:
    added = removed = 0
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return DiffStats(added=added, removed=removed)


def render_lines(diff: str, *, max_lines: int = 0) -> list[tuple[str, str]]:
    """``(kind, line)`` pairs where kind is 'add' | 'del' | 'ctx' | 'meta'.

    The TUI maps kinds to the ``diff_added``/``diff_removed``/``diff_context`` tokens that already
    exist in the theme.
    """
    out: list[tuple[str, str]] = []
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            kind = "meta"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "del"
        else:
            kind = "ctx"
        out.append((kind, line))
        if max_lines and len(out) >= max_lines:
            out.append(("meta", f"… {len(diff.splitlines()) - max_lines} more diff lines"))
            break
    return out
