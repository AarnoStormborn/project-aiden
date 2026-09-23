"""Aiden's terminal UI: a renderer over the harness event stream.

Layering, and why it is this way round (research/06 §Decision, architecture §10):

    loop.py  --emits-->  Event  --reduce-->  Transcript (cells)  --render-->  Lines  -->  writer

- The model (``model.py``) is a pure reducer: no terminal, no I/O.
- The renderer (``render.py``) maps cells to lines at a known width.
- The writer (``writer.py``) is the only module that knows escape sequences.
- ``plain.py`` is the second renderer, for pipes and screen readers.
- ``golden.py`` replays recorded events into frames, so the UI is snapshot-testable headless.

The ``Cell -> Lines`` boundary is deliberate: it keeps a Textual or desktop renderer possible
without touching the reducer, and it is what makes the perf budget measurable.
"""

from .model import (
    ABORTED,
    ERROR,
    OK,
    PENDING,
    RUNNING,
    AssistantText,
    Cell,
    Notice,
    RunSummary,
    Thinking,
    ToolCall,
    Transcript,
    TurnMarker,
    UserPrompt,
)
from .theme import TOKENS, Theme

__all__ = [
    "ABORTED",
    "ERROR",
    "OK",
    "PENDING",
    "RUNNING",
    "TOKENS",
    "AssistantText",
    "Cell",
    "Notice",
    "RunSummary",
    "Theme",
    "Thinking",
    "ToolCall",
    "Transcript",
    "TurnMarker",
    "UserPrompt",
]
