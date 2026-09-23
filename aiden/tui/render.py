"""Rendering: cells to lines.

The ``Cell -> Lines`` boundary is load-bearing (research/06 §Decision): it is what lets a
Textual or desktop renderer host the same model later, and what lets the golden tests assert on
plain strings instead of escape sequences.

``rich`` is used as a *segment producer*, never as the app: it converts markdown and syntax to
styled text at a known width, and this module decides where those lines go. A whole cell's
render is cached by ``(cell, width, theme)`` because the spec budgets "model render is cached
per cell; only the tail repaints".
"""

from __future__ import annotations

import io
import textwrap
from dataclasses import dataclass

from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax

from . import model
from .theme import Theme

GUTTER = "  "  # LIVE_PREFIX_COLS = 2: every continuation line, and cell padding


@dataclass(frozen=True)
class Rendered:
    """A cell's lines, split so the caller knows what may be committed."""

    lines: list[str]
    #: Number of leading lines that are stable (safe to commit into scrollback).
    stable: int

    @property
    def tail(self) -> list[str]:
        return self.lines[self.stable :]


def render_cell(cell: model.Cell, width: int, theme: Theme) -> Rendered:
    """Render one cell at ``width``. Dispatches on cell type."""
    if isinstance(cell, model.UserPrompt):
        return _user_prompt(cell, width, theme)
    if isinstance(cell, model.AssistantText):
        return _assistant(cell, width, theme)
    if isinstance(cell, model.Thinking):
        return _thinking(cell, width, theme)
    if isinstance(cell, model.ToolCall):
        return _tool_call(cell, width, theme)
    if isinstance(cell, model.Notice):
        return _notice(cell, width, theme)
    if isinstance(cell, model.TurnMarker):
        return _turn_marker(cell, width, theme)
    if isinstance(cell, model.RunSummary):
        return _run_summary(cell, width, theme)
    return Rendered([], 0)


def render_all(cells: list[model.Cell], width: int, theme: Theme) -> list[str]:
    """Render a cell list, separating cells with exactly one blank line.

    Vertical whitespace is the primary separator in the transcript (research/06 §Space rhythm);
    there are no vertical borders, so cells would otherwise run together.
    """
    out: list[str] = []
    for cell in cells:
        lines = render_cell(cell, width, theme).lines
        if not lines:
            continue
        if out:
            out.append("")
        out.extend(lines)
    return out


def frame(
    cells: list[model.Cell],
    width: int,
    theme: Theme,
    *,
    tail_text: str = "",
    tail_width: int | None = None,
) -> list[str]:
    """Full frame: committed cells plus the mutable tail as its own lines.

    The tail is rendered separately because it is the only part that repaints; keeping it out of
    the cell list is what makes "paint changed lines only" possible.
    """
    lines = render_all(cells, width, theme)
    if tail_text:
        tail_lines = _plain_lines(tail_text, tail_width or width, theme)
        lines.extend(tail_lines)
    return lines


# --------------------------------------------------------------------------- cells


def _user_prompt(cell: model.UserPrompt, width: int, theme: Theme) -> Rendered:
    marker = theme.glyphs.user
    body = _wrap(cell.text, width - len(GUTTER) - 2)
    # Trailing separation is render_all's job, so cells are spaced uniformly.
    lines = _guttered(body, theme, first=f"{marker} ", style_token="user_text")
    return Rendered([paint(line, "user_text", theme) for line in lines], 1)


def _assistant(cell: model.AssistantText, width: int, theme: Theme) -> Rendered:
    # Streamed text is rendered as plain prose while incomplete: re-parsing markdown on every
    # delta is the expensive path, and the spec requires re-parsing only completed lines.
    lines = [paint(line, "text", theme) for line in _plain_lines(cell.text, width, theme)]
    if cell.status == model.ABORTED:
        lines.append(paint(f"{GUTTER}{theme.glyphs.error} aborted", "error", theme))
    return Rendered(lines, len(lines))


def _thinking(cell: model.Thinking, width: int, theme: Theme) -> Rendered:
    glyph = theme.glyphs.thinking
    tokens = max(1, len(cell.text) // 4)
    header = f"{glyph} thinking · {tokens:,} tok"
    header += f" ({theme.glyphs.expanded})" if not cell.collapsed else " (t to expand)"
    lines = [paint(header, "thinking_text", theme)]
    if not cell.collapsed:
        lines.extend(
            paint(line, "thinking_text", theme) for line in _plain_lines(cell.text, width, theme)
        )
    return Rendered(lines, len(lines))


def _tool_call(cell: model.ToolCall, width: int, theme: Theme) -> Rendered:
    glyph = {
        model.OK: theme.glyphs.ok,
        model.ERROR: theme.glyphs.error,
        model.RUNNING: theme.glyphs.running,
        model.ABORTED: theme.glyphs.error,
    }.get(cell.status, theme.glyphs.pending)

    dot = theme.glyphs.ellipsis
    args = ", ".join(f"{k}={_short(v, dot)}" for k, v in cell.arguments.items())
    timing = f" {cell.duration_ms}ms" if cell.duration_ms else ""
    suffix = ""
    if cell.skipped:
        suffix = "  (skipped: truncated args)"
    elif cell.status == model.ABORTED:
        suffix = "  (aborted)"
    elif cell.truncated:
        suffix = "  (output truncated)"

    status_token = {
        model.OK: "tool_ok",
        model.ERROR: "tool_err",
        model.RUNNING: "tool_pending",
        model.ABORTED: "tool_err",
    }.get(cell.status, "tool_pending")
    header = f"{glyph} {cell.name}({args}){timing}{suffix}"
    lines = [paint(_fit(header, width, dot), status_token, theme)]

    if cell.output:
        visible = cell.output.splitlines()
        hidden = 0
        if not cell.expanded and len(visible) > cell.visible_lines:
            hidden = len(visible) - cell.visible_lines
            visible = visible[: cell.visible_lines]
        for line in visible:
            lines.extend(
                paint(piece, "tool_output", theme) for piece in _wrapped(line, GUTTER, width, theme)
            )
        if hidden:
            lines.append(
                paint(
                    f"{GUTTER}{theme.glyphs.ellipsis} {hidden} more (e to expand)",
                    "dim",
                    theme,
                )
            )
    return Rendered(lines, len(lines))


def _notice(cell: model.Notice, width: int, theme: Theme) -> Rendered:
    glyph = theme.glyphs.warning if cell.level in {"warning", "error"} else theme.glyphs.pending
    tokens = {
        "error": "error",
        "warning": "warning",
        "info": "muted",
    }
    token = tokens.get(cell.level, "muted")
    plain = f"{glyph} {cell.text}"
    # A harness notice is prose; clipping it would hide the instruction it exists to deliver.
    lines = [paint(line, token, theme) for line in _wrapped(plain, "", width, theme)]
    return Rendered(lines, len(lines))


def _turn_marker(cell: model.TurnMarker, width: int, theme: Theme) -> Rendered:
    # Spec: horizontal rules only at turn boundaries, and only at >= 100 columns. Below that the
    # label alone carries the boundary and the width is better spent on content.
    if width < 100:
        return Rendered([paint(f"{theme.glyphs.pending} turn {cell.turn}", "border", theme)], 1)
    label = f"── turn {cell.turn} "
    rule = label + "─" * max(0, width - len(label) - 1)
    return Rendered([paint(_fit(rule, width, theme.glyphs.ellipsis), "border", theme)], 1)


def _run_summary(cell: model.RunSummary, width: int, theme: Theme) -> Rendered:
    parts = [f"turns {cell.turns}", f"tools {cell.tool_calls}"]
    u = cell.usage
    if u.input_tokens or u.output_tokens:
        parts.append(f"tok {u.input_tokens:,}/{u.output_tokens:,}")
    if u.reasoning_tokens:
        parts.append(f"reasoning {u.reasoning_tokens:,}")
    # The HUD must never lie: unknown cost is an em dash, not $0.00.
    parts.append(f"${cell.cost_usd:.4f}" if cell.cost_usd else "cost —")
    if cell.stop_reason and cell.stop_reason != "end_turn":
        parts.append(f"stop {cell.stop_reason}")

    dot = theme.glyphs.ellipsis
    lines = [paint(_fit("  " + " · ".join(parts), width, dot), "muted", theme)]
    if cell.error:
        lines.append(
            paint(_fit(f"{GUTTER}{theme.glyphs.error} {cell.error}", width, dot), "error", theme)
        )
    if cell.session_path:
        lines.append(
            paint(
                f"{GUTTER}{_elide_left(cell.session_path, width - len(GUTTER), dot)}",
                "dim",
                theme,
            )
        )
    # Final gap so the shell prompt does not butt against the summary.
    lines.append("")
    return Rendered(lines, len(lines))


# --------------------------------------------------------------------------- helpers


def plain_lines(text: str, width: int, theme: Theme) -> list[str]:
    """Render streamed **prose** to fitted lines, wrapping rather than clipping.

    Public because the driver commits stable stream lines through it: the lines it writes to
    scrollback must be produced by the same function that would have drawn them live, or the two
    copies would diverge and text would visibly change as it commits.

    Wrapping is essential here, not cosmetic. Assistant text arrives as one long paragraph per
    markdown block, and an earlier version clipped it with the generic ``_fit`` — the transcript
    showed answers ending mid-sentence with a stray ellipsis, which reads as a bug in the model
    rather than a display choice. Indentation is preserved so lists and code stay readable.
    """
    if not text:
        return []
    out: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            out.append("")
            continue
        indent = line[: len(line) - len(line.lstrip())]
        available = max(1, width - len(indent))
        pieces = textwrap.wrap(
            line.strip(),
            width=available,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=False,
        ) or [""]
        out.extend(indent + piece for piece in pieces)
    return out


# Kept as the internal alias used by the cell renderers.
_plain_lines = plain_lines


def paint(text: str, token: str, theme: Theme) -> str:
    """Prefix ``text`` with the token's SGR code. No-op when colour is off.

    Per line, not per span: the writer emits a reset at the end of every line, so a line-level
    prefix is both sufficient and safe against a dropped frame leaking colour.
    """
    prefix = theme.ansi(token)
    return f"{prefix}{text}" if prefix else text


def _wrapped(line: str, prefix: str, width: int, theme: Theme) -> list[str]:
    """Wrap ``line`` to the frame width, prefixing every piece (and continuation) with ``prefix``.

    Tool output is file contents and grep hits, where a clipped line is close to useless: you
    cannot read the rest of a line of code, and the ellipsis only tells you something is missing.
    Wrapping keeps the full text on screen and still marks hidden *source* lines separately.
    """
    available = width - len(prefix)
    if available <= 0 or not line.strip():
        return [""] if not line.strip() else [_fit(f"{prefix}{line}", width, theme.glyphs.ellipsis)]

    indent = line[: len(line) - len(line.lstrip())]
    pieces = textwrap.wrap(
        line.strip(),
        width=max(1, available - len(indent)),
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=False,
    ) or [""]
    return [f"{prefix}{indent}{piece}" for piece in pieces]


def _guttered(body: str, theme: Theme, *, first: str, style_token: str) -> list[str]:
    """Prefix the first line with ``first`` and indent continuations by the gutter."""
    out: list[str] = []
    for index, line in enumerate(body.splitlines() or [""]):
        prefix = first if index == 0 else GUTTER
        out.append(f"{prefix}{line}")
    return out


def _wrap(text: str, width: int) -> str:
    if width <= 0:
        return text
    import textwrap

    return "\n".join(
        textwrap.wrap(
            text,
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=False,
        )
        or [""]
    )


def _short(value: object, ellipsis: str = "…", limit: int = 44) -> str:
    text = str(value).replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 1] + ellipsis


def _elide_left(text: str, width: int, ellipsis: str = "…") -> str:
    """Keep the end of a long path: the filename is the useful part, the prefix is not."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    keep = max(0, width - len(ellipsis))
    return ellipsis + text[-keep:] if keep else ellipsis


def _fit(line: str, width: int, ellipsis: str = "…") -> str:
    """Hard-clip to width, using the theme's ellipsis so ASCII mode stays ASCII."""
    if width <= 0:
        return ""
    if len(line) <= width:
        return line
    return line[: max(0, width - 1)] + ellipsis


def markdown_block(text: str, width: int, theme: Theme) -> list[str]:
    """Render completed markdown to lines at a fixed width.

    Used when a turn finalizes: the streamed plain text is replaced by a proper markdown render
    once, at the end, rather than on every delta.
    """
    sink = io.StringIO()
    console = Console(
        file=sink,
        width=max(20, width),
        force_terminal=theme.colour,
        color_system="truecolor" if theme.colour else None,
        no_color=not theme.colour,
        highlight=False,
    )
    console.print(Markdown(text, justify="left"), end="")
    return sink.getvalue().rstrip("\n").splitlines()


def syntax_block(code: str, lexer: str, width: int, theme: Theme) -> list[str]:
    sink = io.StringIO()
    console = Console(
        file=sink,
        width=max(20, width),
        force_terminal=theme.colour,
        color_system="truecolor" if theme.colour else None,
        no_color=not theme.colour,
    )
    console.print(
        Syntax(code, lexer, theme="ansi_dark" if theme.dark else "ansi_light", word_wrap=True),
        end="",
    )
    return sink.getvalue().rstrip("\n").splitlines()
