"""Golden-frame testing: record events, replay them, compare frames.

The spec requires that the UI be snapshot-testable at three widths (research/06 §What great
agent UIs do #17: "If your UI can't be snapshotted, it will regress"). This module is the
mechanism:

1. ``RecordingEventSink`` writes a run's events to JSONL.
2. ``load_events`` reads them back into the same event objects the loop emits.
3. ``frame_from_events`` reduces them to a frame of plain lines at a given width and scheme.

Because the reducer and renderer are pure, a golden test needs no terminal, no pty and no
sleeps — it is a string comparison. Set ``AIDEN_UPDATE_GOLDEN=1`` to rewrite the snapshots.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from ..events import (
    Diagnostic,
    Event,
    RunFinished,
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnStarted,
    UsageUpdated,
)
from ..providers.types import Usage
from . import render
from .model import Transcript
from .theme import Theme

#: name -> constructor. Explicit rather than reflective so a renamed event is a loud failure.
_EVENT_TYPES: dict[str, type] = {
    "RunStarted": RunStarted,
    "TurnStarted": TurnStarted,
    "TextDelta": TextDelta,
    "ThinkingDelta": ThinkingDelta,
    "ToolCallStarted": ToolCallStarted,
    "ToolCallFinished": ToolCallFinished,
    "Diagnostic": Diagnostic,
    "UsageUpdated": UsageUpdated,
    "RunFinished": RunFinished,
}

#: Widths the spec mandates snapshots for (Codex's diff_gallery precedent).
WIDTHS: tuple[tuple[int, int], ...] = ((80, 24), (94, 35), (120, 40))

GOLDEN_DIR = Path(__file__).resolve().parent.parent.parent / "tests" / "tui" / "golden"


def event_to_dict(event: Event) -> dict:
    payload = dataclasses.asdict(event)
    usage = payload.pop("usage", None)
    if isinstance(usage, Usage):
        payload["usage"] = dataclasses.asdict(usage)
    return {"type": type(event).__name__, **payload}


def event_from_dict(raw: dict) -> Event:
    name = raw.pop("type")
    try:
        cls = _EVENT_TYPES[name]
    except KeyError:
        raise ValueError(f"unknown recorded event type: {name}") from None
    if name == "UsageUpdated" and isinstance(raw.get("usage"), dict):
        raw["usage"] = Usage(**raw["usage"])
    if name == "RunFinished" and isinstance(raw.get("usage"), dict):
        raw["usage"] = Usage(**raw["usage"])
    return cls(**raw)


class RecordingEventSink:
    """EventSink that appends events as JSONL, for later replay."""

    def __init__(self, path: Path | None = None) -> None:
        self.events: list[Event] = []
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")

    def emit(self, event: Event) -> None:
        self.events.append(event)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event_to_dict(event)) + "\n")


def load_events(path: Path) -> list[Event]:
    events: list[Event] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(event_from_dict(json.loads(line)))
    return events


def transcript_from_events(events: list[Event]) -> Transcript:
    transcript = Transcript()
    for event in events:
        transcript.apply(event)
    transcript.finalize()
    return transcript


def frame_from_events(
    events: list[Event], width: int, theme: Theme, *, tail_text: str = ""
) -> list[str]:
    return render.frame(transcript_from_events(events).cells, width, theme, tail_text=tail_text)


def golden_path(name: str, width: int) -> Path:
    return GOLDEN_DIR / f"{name}.{width}.txt"


def compare_or_update(name: str, width: int, lines: list[str]) -> str | None:
    """Return a diff-ish message on mismatch, or ``None`` when the frame matches.

    With ``AIDEN_UPDATE_GOLDEN=1`` the snapshot is (re)written and always matches.
    """
    path = golden_path(name, width)
    actual = "\n".join(lines)
    if os.environ.get("AIDEN_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual + "\n")
        return None

    if not path.exists():
        return f"missing golden file {path} (run with AIDEN_UPDATE_GOLDEN=1 to create it)"

    # Drop exactly one trailing newline: that is the text file's terminator, not a frame line.
    # Stripping all of them (rstrip) would silently lose a legitimate trailing blank line.
    content = path.read_text()
    expected = content[:-1] if content.endswith("\n") else content
    if expected == actual:
        return None

    expected_lines = expected.split("\n")
    for index in range(max(len(expected_lines), len(lines))):
        want = expected_lines[index] if index < len(expected_lines) else "<missing>"
        got = lines[index] if index < len(lines) else "<missing>"
        if want != got:
            return (
                f"{path.name} first differs at line {index + 1}:\n"
                f"  expected: {want!r}\n"
                f"  actual:   {got!r}"
            )
    return f"{path.name} differs (length {len(expected_lines)} vs {len(lines)})"
