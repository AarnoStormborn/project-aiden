"""The live-region writer: differential line output, no flicker.

This is the only module that knows escape sequences (research/06 §Decision: "own the writer").
Its rules come from pi-tui's renderer and Codex's damage tracking:

1. **Never clear the screen.** Move to the first changed row, erase to end, rewrite. A full
   redraw is only for the documented expensive case (width change).
2. **Never re-print finalized lines.** Once a line is committed to scrollback it is the
   terminal's, not ours.
3. **Wrap every frame in synchronized output** (``CSI ?2026h … l``) *when capability detection
   says the terminal supports it*, so no partial frame is ever visible. Textual had to disable
   synchronization in inline mode because it breaks on some terminals, so the gate is the
   feature, not an optimisation.
4. **Reset style at the end of every line**, so a dropped frame cannot leak colour.

The writer is pure: it returns the bytes to write and tracks what it believes is on screen.
That is what makes the steady-state budget ("no change ⇒ zero bytes") testable.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

SYNC_START = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"
RESET = "\x1b[0m"
ERASE_LINE = "\r\x1b[K"
ERASE_TO_END = "\x1b[J"


def cursor_up(count: int) -> str:
    return f"\x1b[{count}A" if count > 0 else ""


def sync_supported(env: Mapping[str, str] | None = None) -> bool:
    """Whether to wrap frames in synchronized output.

    Unknown terminals default to *on* (the common case is a modern emulator) and can be turned
    off with ``AIDEN_SYNC=0`` when a terminal misbehaves — the same escape hatch pi exposes.
    """
    # os.environ is an os._Environ, not a dict[str, str]; Mapping is the honest type.
    resolved: Mapping[str, str] = os.environ if env is None else env
    value = (resolved.get("AIDEN_SYNC") or "").strip().lower()
    if value in {"0", "off", "false", "no"}:
        return False
    if value in {"1", "on", "true", "yes"}:
        return True
    return True


@dataclass(slots=True)
class Damage:
    """What changed between two frames."""

    first_changed: int
    #: True when the difference is not a pure append, so previously shown lines must be rewritten.
    rewrites: bool
    #: True when the frame must be repainted from a clean screen (width change, scroll).
    full_redraw: bool = False

    @property
    def unchanged(self) -> bool:
        return self.first_changed < 0


def compute_damage(previous: list[str], current: list[str]) -> Damage:
    """Compare frames. Returns the first changed index, or ``-1`` when identical."""
    common = min(len(previous), len(current))
    first = -1
    for index in range(common):
        if previous[index] != current[index]:
            first = index
            break

    if first == -1:
        if len(current) == len(previous):
            return Damage(-1, rewrites=False)
        if len(current) > len(previous):
            # Pure append: nothing already on screen changed.
            return Damage(len(previous), rewrites=False)
        # Shrinking: rows below must be erased, so this is a rewrite, not an append.
        return Damage(len(current), rewrites=True)

    return Damage(first, rewrites=True)


@dataclass
class LiveRegion:
    """Tracks the mutable tail of the screen and emits minimal updates."""

    width: int
    sync: bool = True
    #: Set when a width change invalidates wrapping and forces a full repaint.
    _previous: list[str] = field(default_factory=list)
    _drawn: bool = False

    def plan(self, lines: list[str], *, full_redraw: bool = False) -> str:
        """Return the bytes to update the live region to ``lines``."""
        if full_redraw:
            damage = Damage(0, rewrites=True, full_redraw=True)
        else:
            damage = compute_damage(self._previous, lines)

        if damage.unchanged:
            self._previous = list(lines)
            return ""

        body: list[str] = []

        if damage.full_redraw or not self._drawn:
            # First paint or width change: erase what we own and redraw it.
            if self._drawn and self._previous:
                body.append(cursor_up(len(self._previous)))
            body.append(ERASE_TO_END)
        else:
            # Move back to the first changed line and rewrite from there.
            rows_below_top = len(self._previous) - damage.first_changed
            if damage.rewrites and rows_below_top > 0:
                body.append(cursor_up(rows_below_top))
            elif not self._previous:
                body.append(ERASE_TO_END)

        if damage.rewrites:
            body.append(ERASE_TO_END if not self._previous else "")
            for line in lines[damage.first_changed :]:
                body.append(f"{line}{RESET}\n")
        else:
            for line in lines[damage.first_changed :]:
                body.append(f"{line}{RESET}\n")

        # The cursor sits at the start of the row after the last written line; move back up so
        # the next frame can address the region by its top.
        body.append(cursor_up(len(lines) - damage.first_changed))
        body.append(f"\r{ERASE_TO_END}")

        self._previous = list(lines)
        self._drawn = True
        payload = "".join(body)
        if not self.sync:
            return payload
        return f"{SYNC_START}{payload}{SYNC_END}"

    def resize(self, width: int) -> None:
        """Width change invalidates every wrapped line; the caller must repaint fully."""
        if width != self.width:
            self.width = width

    def forget(self) -> None:
        """Drop belief about screen contents (e.g. after something else wrote to stdout)."""
        self._previous = []
        self._drawn = False

    @property
    def drawn_lines(self) -> list[str]:
        return list(self._previous)


def commit(lines: list[str]) -> str:
    """Emit finished lines into real scrollback, outside the live region.

    Committed lines are newline-terminated and style-reset so terminal selection, tmux copy and
    Cmd-F keep working on them (research/06 §What great agent UIs do).
    """
    return "".join(f"{line}{RESET}\n" for line in lines)
