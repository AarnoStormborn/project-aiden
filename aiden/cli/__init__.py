"""Aiden command-line entry points."""

from __future__ import annotations

import sys

USAGE = """aiden <command> [args]

commands:
  tui              interactive session in the terminal (needs a tty)
  ask "question"   ask one question and exit
  eval             mine tasks from this repo, run them, and gate a harness change
  providers        inspect, authenticate and refresh the provider suite
  sessions         list recorded sessions for this project
  version          print the Aiden version

Run `aiden tui --help`, `aiden ask --help` or `aiden providers --help` for details."""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.strip())
        return 0
    command, rest = argv[0], argv[1:]

    if command == "eval":
        from .eval import main as eval_main

        return eval_main(rest)
    if command == "providers":
        from .providers import main as providers_main

        return providers_main(rest)
    if command == "tui":
        from .tui import main as tui_main

        return tui_main(rest)
    if command == "ask":
        from .ask import main as ask_main

        return ask_main(rest)
    if command == "sessions":
        from .sessions import main as sessions_main

        return sessions_main(rest)
    if command == "version":
        from .. import __version__

        print(f"aiden {__version__}")
        return 0

    print(f"unknown command: {command}\n\n{USAGE.strip()}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
