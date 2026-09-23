"""Aiden command-line entry points."""

from __future__ import annotations

import sys

USAGE = """aiden <command> [args]

commands:
  ask "question"   ask a question about the current repository (v0.1 harness)
  providers        inspect, authenticate and refresh the provider suite
  sessions         list recorded sessions for this project
  version          print the Aiden version

Run `aiden ask --help` or `aiden providers --help` for details."""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.strip())
        return 0
    command, rest = argv[0], argv[1:]

    if command == "providers":
        from .providers import main as providers_main

        return providers_main(rest)
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
