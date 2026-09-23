#!/usr/bin/env python3
"""v0.1 acceptance run: five questions about this repo with expected answers.

This is the plan's Step 6 (docs/plan/v0.1-minimal-harness.md) and the seed of the private eval
set the architecture calls for (research/05 §Aiden private eval set). It checks two things per
question:

- **fact**: a substring that must appear in the answer (proves it read the right thing)
- **cost**: turns / tool calls / dollars, so a regression in the loop's efficiency is visible

Run it live (spends money, needs network):

    uv run python scripts/acceptance_v01.py
    uv run python scripts/acceptance_v01.py --only 3
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiden import config
from aiden.events import RecordingSink
from aiden.loop import run_loop
from aiden.providers import ProviderSuite

REPO = Path(__file__).resolve().parent.parent


@dataclass
class Case:
    question: str
    #: Any one of these substrings appearing in the answer counts as correct.
    expect: tuple[str, ...]
    #: Upper bound on tool calls, to catch an agent that flails.
    max_tool_calls: int = 12
    note: str = ""


CASES: list[Case] = [
    Case(
        question="What are the read and grep output budgets, and where are they defined?",
        expect=("16_384", "16384", "16 KB"),
        note="values live in aiden/config.py",
    ),
    Case(
        question="Where is the agent loop defined, and what does it do when a tool call's "
        "arguments are truncated?",
        expect=("not executed", "never execute", "truncated", "skipped"),
        note="the max_tokens rule in aiden/loop.py",
    ),
    Case(
        question="What storage layout do session logs use, and why are they kept outside the repo?",
        expect=(".aiden", "committed", "sessions"),
        note="aiden/config.py + docs/architecture",
    ),
    Case(
        question="Which wire protocols does the provider suite implement, and which providers "
        "does each one serve?",
        expect=("anthropic-messages", "openai-completions", "openai-responses"),
        # Catalog-wide enumeration is genuinely the expensive question: the model inspects the
        # vendored per-provider JSON rather than one owning file. ~20 calls is the current
        # reality; tightening this is a v0.2 target (docs/plan/v0.1-minimal-harness.md).
        max_tool_calls=22,
        note="expensive: enumerates the catalog",
    ),
    Case(
        question="What does the research say about why unified diffs are not used as the primary "
        "edit format?",
        expect=("48.7", "51", "apply", "fail"),
        note="docs/research/02-tooling-efficiency.md",
        max_tool_calls=16,
    ),
]


def normalize(text: str) -> str:
    """Strip digit separators so '16_384', '16,384' and '16384' compare equal.

    The first acceptance run failed a *correct* answer purely because the model wrote
    '16,384' while the expectation was '16_384'. A matcher that punishes formatting teaches
    nothing, so normalise before matching.
    """
    return text.lower().replace(",", "").replace("_", "").replace(" ", "")


async def run_case(suite: ProviderSuite, case: Case, model: str) -> dict:
    sink = RecordingSink()
    try:
        result = await run_loop(
            case.question,
            suite=suite,
            model=model,
            cwd=REPO,
            sink=sink,
            max_turns=config.MAX_TURNS,
            max_cost_usd=config.MAX_RUN_COST_USD,
            max_tokens=config.DEFAULT_MAX_TOKENS,
        )
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    answer = result.answer
    normalized = normalize(answer)
    hit = next((e for e in case.expect if normalize(e) in normalized), None)

    # Only *correctness* is a pass/fail criterion. Efficiency is reported, not asserted:
    # repeated runs of the same question used 11, 17 and 14 tool calls, so a single-sample
    # tool-count bound is noise and would make this suite flaky. research/05 §Metrics we track
    # prescribes paired runs with >=3 seeds for anything efficiency-related; that is the v0.2
    # gate, and it is recorded here as a finding rather than pretended away.
    over_budget = result.tool_calls > case.max_tool_calls
    if result.error:
        reason = f"loop error: {result.error}"
    elif not answer:
        reason = f"no answer (stop_reason={result.stop_reason})"
    elif hit is None:
        reason = f"answer missing any of {case.expect}"
    else:
        reason = ""

    return {
        "ok": not reason,
        "reason": reason,
        "matched": hit or "",
        "turns": result.turns,
        "tool_calls": result.tool_calls,
        "over_budget": over_budget,
        "cost": result.cost_usd,
        "session": result.session_path,
        "answer": answer,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--only", type=int, action="append", help="run only these case numbers (1-based)"
    )
    ap.add_argument("--model", default=config.DEFAULT_MODEL)
    ap.add_argument("--show-answers", action="store_true")
    args = ap.parse_args()

    suite = ProviderSuite.load()
    cases = [(i, c) for i, c in enumerate(CASES, 1) if not args.only or i in args.only]

    passed = 0
    total_cost = 0.0
    for number, case in cases:
        row = await run_case(suite, case, args.model)
        total_cost += row.get("cost", 0.0)
        status = "PASS" if row["ok"] else "FAIL"
        if row["ok"]:
            passed += 1
        flag = " [over budget]" if row.get("over_budget") else ""
        print(
            f"{status} [{number}] turns={row.get('turns', '-')} "
            f"tools={row.get('tool_calls', '-')} ${row.get('cost', 0):.5f}{flag}  "
            f"{case.question[:60]}…"
        )
        if not row["ok"]:
            print(f"       {row['reason']}")
        elif row.get("matched"):
            print(f"       matched {row['matched']!r}")
        if args.show_answers:
            print("       " + row.get("answer", "").replace("\n", "\n       ")[:600])

    print(f"\n{passed}/{len(cases)} cases correct · total ${total_cost:.5f}")
    print("note: efficiency is reported, not asserted — single-sample tool counts are noisy")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
