"""``aiden ask`` — the v0.1 interface: a question in, an answer plus a cost report out.

Deliberately plain stdout. The inline-first TUI is M3 (docs/research/06-ui-ux.md); because the
loop emits events rather than printing, that TUI will be a new sink here, not a rewrite.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .. import config
from ..events import Diagnostic, TextDelta, ThinkingDelta
from ..loop import run_loop
from ..providers import ProviderError, ProviderSuite
from ..session import SessionStore
from ..tui.theme import no_colour

C = {
    "dim": "\033[2m",
    "bold": "\033[1m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "green": "\033[32m",
    "cyan": "\033[36m",
    "reset": "\033[0m",
}


def _use_colour() -> bool:
    return sys.stdout.isatty()


def paint(text: str, colour: str) -> str:
    if not _use_colour():
        return text
    return f"{C.get(colour, '')}{text}{C['reset']}"


class ConsoleSink:
    """Streams text as it arrives; prints tool activity and diagnostics to stderr.

    Assistant text goes to stdout so ``aiden ask "..." > answer.txt`` yields clean output;
    everything else is progress information and belongs on stderr.
    """

    def __init__(self, *, verbose: bool = False, show_thinking: bool = False) -> None:
        self.verbose = verbose
        self.show_thinking = show_thinking
        self._thinking_header_shown = False
        self.tool_calls = 0

    def emit(self, event) -> None:
        from ..events import ToolCallFinished, ToolCallStarted, TurnStarted

        if isinstance(event, TextDelta):
            sys.stdout.write(event.text)
            sys.stdout.flush()
        elif isinstance(event, ThinkingDelta):
            if self.show_thinking:
                if not self._thinking_header_shown:
                    print(paint("\n[thinking]", "dim"), file=sys.stderr)
                    self._thinking_header_shown = True
                sys.stderr.write(paint(event.text, "dim"))
                sys.stderr.flush()
        elif isinstance(event, TurnStarted) and self.verbose:
            print(paint(f"\n--- turn {event.turn} ---", "dim"), file=sys.stderr)
        elif isinstance(event, ToolCallStarted):
            self.tool_calls += 1
            args = ", ".join(f"{k}={_short(v)}" for k, v in event.arguments.items())
            print(
                paint(f"  → {event.name}({args})", "cyan"),
                file=sys.stderr,
            )
        elif isinstance(event, ToolCallFinished) and (self.verbose or event.is_error):
            status = "error" if event.is_error else "ok"
            extra = " [skipped: truncated args]" if event.skipped else ""
            chars = "0" if event.skipped else f"{event.output_chars:,}"
            colour = "red" if event.is_error else "dim"
            print(
                paint(
                    f"    {status}{extra} {event.duration_ms}ms, {chars} chars",
                    colour,
                ),
                file=sys.stderr,
            )
            if self.verbose and event.output:
                first = event.output.strip().splitlines()[0]
                print(paint(f"    {first[:110]}", "dim"), file=sys.stderr)
        elif isinstance(event, Diagnostic):
            colour = "red" if event.level == "error" else "yellow"
            print(paint(f"  ! {event.message}", colour), file=sys.stderr)


def _wants_tui(args: argparse.Namespace) -> bool:
    """TUI only when it can be seen: a terminal, colour-capable, and not machine-readable."""
    if args.json or args.tui == "off":
        return False
    if args.tui == "on":
        return True
    if no_colour() or os.environ.get("AIDEN_TUI") == "plain":
        return False
    return sys.stdout.isatty()


def _make_sink(args: argparse.Namespace):
    """Pick the renderer. The loop is unaware of which one it got."""
    if not _wants_tui(args):
        return (
            ConsoleSink(verbose=args.verbose, show_thinking=args.show_thinking),
            _NullGuard(),
            False,
        )

    from ..tui.driver import InterruptGuard, TUIDriver

    driver = TUIDriver(show_thinking=args.show_thinking)
    return driver, InterruptGuard(driver), True


class _NullGuard:
    """No-op context manager, so the call site needs no branching."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aiden ask",
        description="Ask a question about the current repository.",
    )
    parser.add_argument("question", nargs="*", help="the question to ask")
    parser.add_argument("--cwd", type=Path, default=None, help="working directory (default: $PWD)")
    parser.add_argument("--model", default=config.DEFAULT_MODEL, help="provider/model[:level]")
    parser.add_argument("--max-turns", type=int, default=config.MAX_TURNS)
    parser.add_argument("--max-cost", type=float, default=config.MAX_RUN_COST_USD, dest="max_cost")
    parser.add_argument("--max-tokens", type=int, default=config.DEFAULT_MAX_TOKENS)
    parser.add_argument("--thinking", default=config.DEFAULT_THINKING_LEVEL)
    parser.add_argument("--verbose", "-v", action="store_true", help="show tool results and turns")
    parser.add_argument("--show-thinking", action="store_true", help="print reasoning text")
    parser.add_argument("--no-session", action="store_true", help="do not write a session file")
    parser.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    parser.add_argument(
        "--tui",
        choices=["auto", "on", "off"],
        default="auto",
        help="inline TUI (auto = on when stdout is a terminal)",
    )
    parser.add_argument("--system-prompt", type=Path, default=None, help="override the base prompt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    question = " ".join(args.question).strip()
    if not question:
        print("error: no question given", file=sys.stderr)
        return 2

    cwd = (args.cwd or Path.cwd()).resolve()
    if not cwd.is_dir():
        print(f"error: --cwd is not a directory: {cwd}", file=sys.stderr)
        return 2

    sink, interrupt, used_tui = _make_sink(args)

    try:
        return asyncio.run(_run(args, question, cwd, sink, interrupt, used_tui))
    except KeyboardInterrupt:
        # SIGINT surfaces from ``asyncio.run`` in runners.py, *not* from the awaited coroutine,
        # so catching it inside ``_run`` never fires. Marking the transcript aborted here
        # commits the partial answer instead of leaving it in a live region that is about to be
        # discarded, and keeps a traceback off the user's screen.
        abort = getattr(sink, "abort", None)
        if callable(abort):
            abort()
        print(paint("\ninterrupted", "yellow"), file=sys.stderr)
        return 130


async def _run(
    args: argparse.Namespace,
    question: str,
    cwd: Path,
    sink,
    interrupt,
    used_tui: bool,
) -> int:
    suite = ProviderSuite.load()
    try:
        model = suite.registry.resolve(args.model)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    session = None if args.no_session else SessionStore.create(cwd=cwd, model=model.model.ref)
    system_prompt = args.system_prompt.read_text() if args.system_prompt else None

    try:
        with interrupt:
            result = await run_loop(
                question,
                suite=suite,
                model=model.model,
                cwd=cwd,
                sink=sink,
                session=session,
                max_turns=args.max_turns,
                max_cost_usd=args.max_cost,
                max_tokens=args.max_tokens,
                thinking_level=model.thinking_level or args.thinking,
                system_prompt=system_prompt,
            )
    except ProviderError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1

    if args.json:
        import json

        print(
            json.dumps(
                {
                    "answer": result.answer,
                    "stop_reason": result.stop_reason,
                    "turns": result.turns,
                    "tool_calls": result.tool_calls,
                    "usage": {
                        "input_tokens": result.usage.input_tokens,
                        "output_tokens": result.usage.output_tokens,
                        "cache_read_tokens": result.usage.cache_read_tokens,
                        "cache_write_tokens": result.usage.cache_write_tokens,
                        "reasoning_tokens": result.usage.reasoning_tokens,
                    },
                    "cost_usd": round(result.cost_usd, 6),
                    "session_path": result.session_path,
                    "diagnostics": result.diagnostics,
                    "error": result.error,
                },
                indent=2,
            )
        )
    elif not used_tui:
        # The TUI already rendered a summary cell into the transcript; printing the CLI report
        # as well duplicated every number on screen.
        print()
        _print_report(result)

    if result.error:
        return 1
    return 0 if result.answer else 1


def _print_report(result) -> None:
    """The cost/turn summary the plan requires every run to print."""
    u = result.usage
    parts = [
        f"turns {result.turns}",
        f"tool calls {result.tool_calls}",
        f"tokens in/out {u.input_tokens:,}/{u.output_tokens:,}",
    ]
    if u.reasoning_tokens:
        parts.append(f"reasoning {u.reasoning_tokens:,}")
    if u.cache_read_tokens:
        parts.append(f"cache read {u.cache_read_tokens:,}")
    parts.append(f"cost ${result.cost_usd:.4f}")

    print(paint(" · ".join(parts), "dim"), file=sys.stderr)
    if result.stop_reason not in ("end_turn",):
        print(paint(f"stop: {result.stop_reason}", "yellow"), file=sys.stderr)
    for note in result.diagnostics:
        print(paint(f"note: {note}", "yellow"), file=sys.stderr)
    if result.session_path:
        print(paint(f"session: {result.session_path}", "dim"), file=sys.stderr)


def _short(value: object, limit: int = 48) -> str:
    text = str(value).replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = ["ConsoleSink", "build_parser", "main", "paint"]
