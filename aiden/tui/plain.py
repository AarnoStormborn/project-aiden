"""The plain renderer: a linear, escape-free transcript.

This is not a downgrade — it is a required surface (research/06 §Accessibility budget and
§Modes). Pipes, CI, ``less``, screen readers and ``AIDEN_TUI=plain`` all need a transcript that
is linear in reading order, complete (no "… 12 more"), and free of cursor movement.

It shares the cell model with the TUI, which is the point: one model, two renderers behind the
same interface. Anything that renders differently here is a rendering bug, not a missing feature.
"""

from __future__ import annotations

from . import model
from .theme import Theme


def render_cell(cell: model.Cell, width: int, theme: Theme) -> list[str]:
    """Plain-mode lines for one cell. No escapes, no truncation, no width maths."""
    if isinstance(cell, model.UserPrompt):
        return [f"> {cell.text}", ""]
    if isinstance(cell, model.AssistantText):
        return [*cell.text.splitlines(), ""]
    if isinstance(cell, model.Thinking):
        return [f"(thinking, {len(cell.text) // 4:,} tokens)", ""]
    if isinstance(cell, model.ToolCall):
        status = {
            model.OK: "ok",
            model.ERROR: "error",
            model.RUNNING: "running",
            model.ABORTED: "aborted",
        }.get(cell.status, cell.status)
        args = ", ".join(f"{k}={v}" for k, v in cell.arguments.items())
        head = f"[tool] {cell.name}({args}) {status}"
        if cell.duration_ms:
            head += f" {cell.duration_ms}ms"
        if cell.skipped:
            head += " (skipped: truncated args)"
        lines = [head]
        # Full output, never elided: a screen reader cannot press 'e' to expand.
        lines.extend(cell.output.splitlines())
        lines.append("")
        return lines
    if isinstance(cell, model.Notice):
        return [f"[{cell.level}] {cell.text}", ""]
    if isinstance(cell, model.TurnMarker):
        return [f"--- turn {cell.turn} ---", ""]
    if isinstance(cell, model.RunSummary):
        u = cell.usage
        parts = [
            f"turns {cell.turns}",
            f"tool calls {cell.tool_calls}",
            f"tokens in/out {u.input_tokens:,}/{u.output_tokens:,}",
        ]
        if u.reasoning_tokens:
            parts.append(f"reasoning {u.reasoning_tokens:,}")
        parts.append(f"cost ${cell.cost_usd:.4f}" if cell.cost_usd else "cost unknown")
        lines = [" · ".join(parts)]
        if cell.stop_reason and cell.stop_reason != "end_turn":
            lines.append(f"stop: {cell.stop_reason}")
        if cell.error:
            lines.append(f"error: {cell.error}")
        if cell.session_path:
            lines.append(f"session: {cell.session_path}")
        return lines
    return []


def render_all(cells: list[model.Cell], width: int, theme: Theme) -> list[str]:
    out: list[str] = []
    for cell in cells:
        out.extend(render_cell(cell, width, theme))
    return out


def render_tail(text: str) -> list[str]:
    """Streaming text in plain mode: printed as it arrives, no live region."""
    return text.splitlines()
