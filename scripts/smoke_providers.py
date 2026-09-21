#!/usr/bin/env python3
"""Live smoke test: one real call per authenticated provider through the suite.

Not part of the test suite (it spends money and needs network). Budget-capped: each call
asks for a one-word answer with a tiny max_tokens.

    uv run python scripts/smoke_providers.py
    uv run python scripts/smoke_providers.py --ref anthropic/claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiden.providers import Message, ProviderSuite, Stop, TextDelta

DEFAULT_REFS = [
    "anthropic/claude-haiku-4-5",
    "openai/gpt-5.4-mini",
    "opencode-go/qwen3.8-flash",
    "commandcode/claude-haiku-4-5-20251001",
    "commandcode/meta/muse-spark-1.3-contributor",
]


async def probe(suite: ProviderSuite, ref: str) -> dict:
    result: dict = {"ref": ref, "ok": False}
    try:
        model = suite.resolve(ref)
    except KeyError as exc:
        result["error"] = f"resolve failed: {exc}"
        return result
    if model.api not in __import__("aiden.providers", fromlist=["available_apis"]).available_apis():
        result["error"] = f"no transport for {model.api}"
        return result

    text, usage, stop = "", None, None
    try:
        async for event in suite.stream(
            model,
            [Message.text("user", "Reply with exactly one word: pong")],
            system="You are a terse test harness. Answer with a single word.",
            # Reasoning models (Muse Spark, DeepSeek) spend most of the budget thinking; a
            # tight cap returns empty text with the truncation diagnostic instead of an answer.
            max_tokens=2_000,
            thinking_level="off",
        ):
            if isinstance(event, TextDelta):
                text += event.text
            elif isinstance(event, Stop):
                usage, stop = event.usage, event
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result.update(
        ok=bool(text.strip()),
        api=model.api,
        text=text.strip()[:60],
        stop_reason=stop.reason if stop else "",
        tokens=(
            f"{usage.input_tokens}/{usage.output_tokens}"
            + (f"+{usage.reasoning_tokens}r" if usage.reasoning_tokens else "")
        )
        if usage
        else "",
        cost=f"${stop.cost_usd:.5f}" if stop else "",
        error=stop.message if stop and stop.reason == "error" else "",
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
        row = await probe(suite, ref)
        status = "OK  " if row["ok"] else "FAIL"
        if not row["ok"]:
            failures += 1
        detail = row.get("text") or row.get("error", "")
        print(
            f"{status} {row['ref']:48s} {row.get('api', ''):20s} "
            f"{row.get('tokens', ''):>12s} {row.get('cost', ''):>9s}  {detail}"
        )
        if row.get("error") and row["ok"]:
            print(f"     warning: {row['error']}")

    print(f"\n{len(refs) - failures}/{len(refs)} providers responded")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
