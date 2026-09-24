"""``aiden eval`` — mine tasks, run them, report, and gate a change.

The gate is the point: it is the function Tier-2 self-update will call before Aiden may replace its own
source, and it exists before anything can propose such a change.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .. import config
from ..eval.mine import mine
from ..eval.report import RunReport, gate
from ..eval.runner import RunOptions, default_agent, run_task
from ..eval.task import load_tasks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiden eval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    mine_cmd = sub.add_parser("mine", help="derive validated tasks from this repository")
    mine_cmd.add_argument("--out", type=Path, default=Path("eval/tasks"))
    mine_cmd.add_argument("--limit", type=int, default=6)
    mine_cmd.add_argument("--per-module", type=int, default=1, dest="per_module")
    mine_cmd.add_argument("--max-tests", type=int, default=15, dest="max_tests")
    mine_cmd.add_argument("--base", default="HEAD", help="commit to derive tasks from")

    run_cmd = sub.add_parser("run", help="run the agent against the tasks and grade it")
    run_cmd.add_argument("--tasks", type=Path, default=Path("eval/tasks"))
    run_cmd.add_argument("--out", type=Path, default=Path("eval/runs/latest.json"))
    run_cmd.add_argument("--label", default="")
    run_cmd.add_argument("--model", default=config.DEFAULT_MODEL)
    run_cmd.add_argument("--seeds", type=int, default=1)
    run_cmd.add_argument("--max-turns", type=int, default=15, dest="max_turns")
    run_cmd.add_argument("--max-cost", type=float, default=1.0, dest="max_cost")
    run_cmd.add_argument("--only", action="append", dest="only", help="task id (repeatable)")

    report_cmd = sub.add_parser("report", help="summarise a saved run")
    report_cmd.add_argument("run", type=Path)

    gate_cmd = sub.add_parser("gate", help="may the candidate replace the baseline?")
    gate_cmd.add_argument("--baseline", type=Path, required=True)
    gate_cmd.add_argument("--candidate", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "mine":
        return cmd_mine(args)
    if args.command == "run":
        return asyncio.run(cmd_run(args))
    if args.command == "report":
        return cmd_report(args)
    if args.command == "gate":
        return cmd_gate(args)
    return 2


# --------------------------------------------------------------------------- mine


def cmd_mine(args: argparse.Namespace) -> int:
    repo = Path.cwd()
    print(f"mining tasks from {repo} at {args.base}")
    tasks, rejections = mine(
        repo,
        base_commit=args.base,
        limit=args.limit,
        per_module=args.per_module,
        max_f2p=args.max_tests,
        on_progress=lambda message: print(f"  {message}"),
    )

    for task in tasks:
        task.save(args.out / f"{task.id}.json")
    print(f"\naccepted {len(tasks)} task(s) into {args.out}")
    for task in tasks:
        print(f"  {task.summary}")
    if rejections:
        print(f"\nrejected {len(rejections)} candidate(s):")
        for rejection in rejections[:12]:
            print(f"  {rejection.candidate}: {rejection.reason}")
        if len(rejections) > 12:
            print(f"  … {len(rejections) - 12} more")
    if not tasks:
        print(
            "\nNo task could be validated. That is a real result: a task is only adopted when the "
            "tests can be shown to fail without the code and pass with it.",
            file=sys.stderr,
        )
        return 1
    return 0


# --------------------------------------------------------------------------- run


async def cmd_run(args: argparse.Namespace) -> int:
    tasks = load_tasks(args.tasks)
    if args.only:
        wanted = set(args.only)
        tasks = [task for task in tasks if task.id in wanted]
    if not tasks:
        print(f"error: no tasks in {args.tasks}", file=sys.stderr)
        return 2

    options = RunOptions(
        model=args.model,
        max_turns=args.max_turns,
        max_cost_usd=args.max_cost,
        seeds=args.seeds,
        on_event=lambda message: print(message),
    )
    agent = default_agent(options)
    print(f"running {len(tasks)} task(s) x {args.seeds} seed(s) with {args.model}")

    report = RunReport(label=args.label, model=args.model, harness_version=config.HARNESS_VERSION)
    from datetime import UTC, datetime

    report.started_at = datetime.now(UTC).isoformat()

    for task in tasks:
        report.results.extend(await run_task(task, agent=agent, options=options, repo=Path.cwd()))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    report.save(args.out)
    print()
    print(report.render())
    print(f"\nwritten to {args.out}")
    return 0 if not any(r.infra_failure for r in report.results) else 1


# --------------------------------------------------------------------------- report / gate


def cmd_report(args: argparse.Namespace) -> int:
    report = RunReport.load(args.run)
    if report.label:
        print(f"# {report.label}")
    print(report.render())
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    baseline = RunReport.load(args.baseline)
    candidate = RunReport.load(args.candidate)
    decision = gate(baseline, candidate)
    print(decision.render())
    return 0 if decision.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
