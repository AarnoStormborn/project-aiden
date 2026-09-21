"""Aiden command-line entry points."""

from __future__ import annotations

import sys

USAGE = """aiden <command> [args]

commands:
  providers    inspect, authenticate and refresh the provider suite
  version      print the Aiden version
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.strip())
        return 0
    command, rest = argv[0], argv[1:]

    if command == "providers":
        from .providers import main as providers_main

        return providers_main(rest)
    if command == "version":
        from .. import __version__

        print(f"aiden {__version__}")
        return 0

    print(f"unknown command: {command}\n\n{USAGE.strip()}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
