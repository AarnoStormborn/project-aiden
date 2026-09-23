"""``aiden sessions`` — inspect what the harness recorded.

The session log is the product (docs/architecture/aiden-architecture.md §7), so being able to
read it without opening JSONL by hand is part of v0.1's usefulness.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..session import entry_to_dict as _entry_to_dict
from ..session import list_sessions, read_entries


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiden sessions", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list sessions for a project")
    listing.add_argument("--cwd", type=Path, default=None)
    listing.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="print a session transcript")
    show.add_argument("session", help="session id or path to a .jsonl file")
    show.add_argument("--cwd", type=Path, default=None)
    show.add_argument("--json", action="store_true", help="raw entries as JSON lines")
    show.add_argument("--tools", action="store_true", help="include tool results in full")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "list":
        return cmd_list(args)
    if args.command == "show":
        return cmd_show(args)
    return 2


def cmd_list(args: argparse.Namespace) -> int:
    sessions = list_sessions(args.cwd)
    if args.json:
        print(json.dumps(sessions, indent=2))
        return 0
    if not sessions:
        print("no sessions recorded for this project yet")
        return 0
    print(f"{'session':26} {'entries':>7} {'size':>8}  {'started':26} model")
    for row in sessions:
        print(
            f"{row['session_id']:26} {row['entries']:>7} {row['size_bytes']:>8}  "
            f"{row['started_at'][:26]:26} {row['model']}"
        )
    print(f"\n{len(sessions)} session(s)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path = Path(args.session)
    if not path.is_file():
        matches = [
            row for row in list_sessions(args.cwd) if row["session_id"].startswith(args.session)
        ]
        if not matches:
            print(f"error: no session matching '{args.session}'", file=sys.stderr)
            return 1
        path = Path(matches[0]["path"])

    entries = list(read_entries(path))
    if args.json:
        for entry in entries:
            print(json.dumps(_entry_to_dict(entry)))
        return 0

    print(f"# {path}\n")
    for entry in entries:
        payload = entry.payload
        label = entry.type
        if label == "user_message":
            print(f"USER      {payload.get('text', '')}")
        elif label == "assistant_message":
            text = (payload.get("text") or "").strip()
            calls = payload.get("tool_calls") or []
            print(f"ASSISTANT {text}")
            for call in calls:
                args_text = json.dumps(call.get("arguments", {}))
                print(f"          -> {call.get('name')}({args_text})")
        elif label == "tool_result":
            output = payload.get("output", "")
            if not args.tools and len(output) > 400:
                output = output[:400] + f"\n          … [{len(output):,} chars total]"
            status = "ERROR" if payload.get("is_error") else "ok"
            print(f"TOOL      {payload.get('name')} [{status}] {payload.get('duration_ms', 0)}ms")
            for line in output.splitlines()[:40]:
                print(f"          {line}")
        elif label == "usage":
            print(
                f"USAGE     in {payload.get('input_tokens', 0):,} "
                f"out {payload.get('output_tokens', 0):,} "
                f"${payload.get('cost_usd', 0):.6f}"
            )
        elif label == "diagnostic":
            print(f"NOTE      {payload.get('message', '')}")
        elif label == "run_end":
            print(
                f"END       {payload.get('stop_reason')} · turns {payload.get('turns')} · "
                f"tool calls {payload.get('tool_calls')} · ${payload.get('cost_usd', 0):.6f}"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
