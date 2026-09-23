"""``aiden tui`` — an interactive session in the terminal.

`aiden ask` answers one question; this stays open. See aiden/tui/repl.py for the loop and
aiden/tui/driver.py for the renderer.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .. import config
from ..tui.repl import AidenSession
from ..tui.theme import Theme


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aiden tui",
        description="Start an interactive Aiden session.",
    )
    parser.add_argument("--cwd", type=Path, default=None, help="working directory (default: $PWD)")
    parser.add_argument("--model", default=config.DEFAULT_MODEL, help="provider/model[:level]")
    parser.add_argument("--max-turns", type=int, default=config.MAX_TURNS)
    parser.add_argument("--max-cost", type=float, default=config.MAX_RUN_COST_USD, dest="max_cost")
    parser.add_argument("--show-thinking", action="store_true", help="print reasoning text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not sys.stdin.isatty():
        print(
            'error: `aiden tui` needs a terminal. Use `aiden ask "question"` for pipes,\n'
            "       or AIDEN_TUI=plain for a linear transcript.",
            file=sys.stderr,
        )
        return 2

    cwd = (args.cwd or Path.cwd()).resolve()
    if not cwd.is_dir():
        print(f"error: --cwd is not a directory: {cwd}", file=sys.stderr)
        return 2

    from ..tui.driver import TUIDriver

    driver = TUIDriver(theme=Theme.from_env(), show_thinking=args.show_thinking)
    session = AidenSession(
        cwd=cwd,
        model=args.model,
        max_turns=args.max_turns,
        max_cost_usd=args.max_cost,
        driver=driver,
    )
    try:
        return asyncio.run(session.run())
    except KeyboardInterrupt:
        # Ctrl-C at the prompt: same treatment as `aiden ask`, mark the transcript aborted and
        # leave without a traceback.
        driver.abort()
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
