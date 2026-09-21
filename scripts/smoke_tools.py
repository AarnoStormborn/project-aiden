#!/usr/bin/env python3
"""Live tool-calling smoke test: proves the tool path across protocols.

Verifies the contract the harness depends on:
  1. the model emits a tool call for our ``ToolSpec`` (schema round-trips),
  2. arguments arrive parsed as JSON,
  3. tool results are accepted on the next turn and the model produces a final answer.

    uv run python scripts/smoke_tools.py
    uv run python scripts/smoke_tools.py --ref anthropic/claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiden.providers import (
    Message,
    ProviderSuite,
    ToolResultPart,
    ToolSpec,
)

READ_TOOL = ToolSpec(
    name="read_file",
    description="Read a file from the repository and return its contents.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "repo-relative file path"}},
        "required": ["path"],
    },
)

FAKE_FILE = (
    "# Research Brief - Project Aiden\n\n"
    "Shared context for every research agent working on this repo.\n"
    "Project Aiden is a personal, learning-oriented agentic harness for coding + research.\n"
)

DEFAULT_REFS = [
    "anthropic/claude-haiku-4-5",
    "openai/gpt-5.4-mini",
    "opencode-go/qwen3.8-flash",
    "opencode-go/deepseek-v4-flash",
    "commandcode/claude-haiku-4-5-20251001",
    "commandcode/meta/muse-spark-1.3-contributor",
]


async def probe(suite: ProviderSuite, ref: str) -> dict:
    result: dict = {"ref": ref, "ok": False}
    model = suite.resolve(ref)
    result["api"] = model.api

    try:
        turn1 = await suite.complete(
            model,
            [Message.text("user", "Read the file docs/BRIEF.md, then say who it is for.")],
            [READ_TOOL],
            system="You are a terse coding agent. Use the provided tool to read files.",
            max_tokens=2_000,
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    if not turn1.tool_calls:
        result["error"] = f"no tool call emitted (stop={turn1.stop_reason}) {turn1.text[:80]!r}"
        return result

    call = turn1.tool_calls[0]
    result["tool"] = call.name
    result["args"] = call.arguments
    if call.name != "read_file":
        result["error"] = f"unexpected tool {call.name!r}"
        return result
    if "path" not in call.arguments:
        result["error"] = f"arguments missing 'path': {call.arguments}"
        return result

    # Feed the result back exactly as the loop will: assistant turn + tool result message.
    convo = [
        Message.text("user", "Read the file docs/BRIEF.md, then say who it is for."),
        turn1.to_message(),
        Message(role="tool", content=[ToolResultPart(call_id=call.id, output=FAKE_FILE)]),
    ]
    try:
        turn2 = await suite.complete(
            model,
            convo,
            [READ_TOOL],
            system="You are a terse coding agent. Use the provided tool to read files.",
            max_tokens=2_000,
        )
    except Exception as exc:
        result["error"] = f"turn 2 failed: {type(exc).__name__}: {exc}"
        return result

    result.update(
        ok=bool(turn2.text.strip()),
        answer=turn2.text.strip()[:70],
        cost=f"${turn1.cost_usd + turn2.cost_usd:.5f}",
        tokens=f"{turn1.usage.input_tokens + turn2.usage.input_tokens}/"
        f"{turn1.usage.output_tokens + turn2.usage.output_tokens}",
        error=turn2.diagnostic if not turn2.text.strip() else "",
    )
    return result


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", action="append", dest="refs")
    args = ap.parse_args()

    suite = ProviderSuite.load()
    refs = args.refs or DEFAULT_REFS
    failures = 0

    for ref in refs:
        try:
            row = await probe(suite, ref)
        except Exception as exc:
            row = {"ref": ref, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

        if not row["ok"]:
            failures += 1
        detail = row.get("answer") or row.get("error", "")
        args_str = row.get("args")
        arg_preview = f" args={args_str}" if args_str else ""
        print(
            f"{'OK  ' if row['ok'] else 'FAIL'} {ref:46s} {row.get('api', ''):20s} "
            f"{row.get('tokens', ''):>10s} {row.get('cost', ''):>9s}{arg_preview}  {detail}"
        )

    print(f"\n{len(refs) - failures}/{len(refs)} providers completed a tool round-trip")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
