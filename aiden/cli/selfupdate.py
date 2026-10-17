"""``aiden selfupdate`` — evaluate a proposed change to Aiden and decide whether to apply it.

This is Tier-2 self-update (architecture §4.5). A proposal is *supplied*: a JSON file naming the files
it would change, why, and what it claims to improve. The proposer is deliberately not built here —
this command is the gate, and a gate that can also write its own inputs is not a gate.

    aiden selfupdate show      proposal.json          read a proposal and its policy verdict
    aiden selfupdate check     proposal.json          static gate only, no eval, no spend
    aiden selfupdate run       proposal.json          full pipeline, with approval before apply

`check` exists because it is the cheap half: it answers "is this change even well-formed" for the
cost of lint and typechecking, which is the right first question and costs nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .. import config
from ..approval import DenyAll
from ..eval.task import load_tasks
from ..selfupdate import EvalSpec, Proposal, check, default_evaluate, load
from ..selfupdate.pipeline import run
from ..tui.approval_ui import PromptApprover


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiden selfupdate", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("show", "read a proposal and report its policy verdict"),
        ("check", "static gate only: lint and types, no eval, no spend"),
        ("run", "the full pipeline, with approval before anything is applied"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("proposal", type=Path)
        if name == "run":
            command.add_argument(
                "--tasks",
                type=Path,
                default=Path("eval/tasks"),
                help="mined tasks to evaluate with",
            )
            command.add_argument("--seeds", type=int, default=1)
            command.add_argument("--model", default=config.DEFAULT_MODEL)
            command.add_argument("--max-turns", type=int, default=15, dest="max_turns")
            command.add_argument("--max-cost", type=float, default=1.0, dest="max_cost")
            command.add_argument(
                "--test-target",
                default="",
                dest="test_target",
                help="pytest arguments for the static gate, e.g. 'tests/test_loop.py -q'",
            )
            command.add_argument(
                "--only",
                action="append",
                dest="only",
                help="task id to evaluate (repeatable); bounds the spend for a trial run",
            )
            command.add_argument(
                "--no-tests",
                action="store_true",
                dest="no_tests",
                help="skip pytest in the static gate (it is the slow part; the eval is the real gate)",
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        proposal = load(args.proposal)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "show":
        return cmd_show(proposal)
    if args.command == "check":
        return asyncio.run(cmd_check(proposal))
    if args.command == "run":
        return asyncio.run(cmd_run(proposal, args))
    return 2


def cmd_show(proposal: Proposal) -> int:
    """Print the proposal and where it stands, writing nothing and calling no model."""
    repo = Path.cwd()
    print(f"{proposal.id}: {proposal.title}")
    print(f"claimed: {proposal.claimed_improvement}")
    if proposal.evidence:
        print("evidence:")
        for item in proposal.evidence:
            print(f"  - {item}")
    print("\nfiles:")
    for path in proposal.changed_paths():
        action = "delete" if proposal.files[path] is None else "write"
        print(f"  {action}: {path}")
    print("\ndiff:")
    print(proposal.render_diff(repo) or "  (no changes)")

    violations = check(proposal.files, repo)
    print("\npolicy: " + ("refused" if violations else "allowed"))
    for violation in violations:
        print(f"  - {violation}")
    return 1 if violations else 0


async def cmd_check(proposal: Proposal) -> int:
    """The static gate alone. No approval, no eval, no writes: a refusal here costs nothing."""
    outcome = await run(
        proposal,
        repo=Path.cwd(),
        evaluator=None,
        spec=None,
        # Nothing can be applied on this path (there is no eval, so nothing can pass), and asking
        # would train the user to approve reflexively — the failure mode the gates exist to prevent.
        approver=DenyAll(),
        apply_on_pass=False,
        # Lint and types only. This matches the subcommand's own promise of a fast, free answer:
        # running the suite here would be minutes of work for a question the eval answers better.
        include_tests=False,
    )
    print(outcome.render())
    return 0 if (outcome.static and outcome.static.passed) else 1


async def cmd_run(proposal: Proposal, args: argparse.Namespace) -> int:
    repo = Path.cwd()
    tasks = load_tasks(args.tasks)
    if args.only:
        wanted = set(args.only)
        tasks = [task for task in tasks if task.id in wanted]
    if not tasks:
        print(
            f"error: no tasks in {args.tasks}; run `aiden eval mine` first, because an unmeasured "
            "self-update is not gated",
            file=sys.stderr,
        )
        return 2

    spec = EvalSpec(
        tasks=tasks,
        seeds=args.seeds,
        model=args.model,
        max_turns=args.max_turns,
        max_cost_usd=args.max_cost,
    )
    # Tier 2 is always human-gated. `--yes` is not offered: that would make the eval the only lock.
    approver = PromptApprover() if sys.stdin.isatty() else DenyAll()

    def say(message: str) -> None:
        print(message, flush=True)

    outcome = await run(
        proposal,
        repo=repo,
        evaluator=default_evaluate(repo, spec, config.AIDEN_HOME / "eval-sessions"),
        spec=spec,
        approver=approver,
        include_tests=not args.no_tests,
        test_target=args.test_target,
        on_event=say,
    )
    print()
    print(outcome.render())
    if outcome.archive_path:
        print(f"archived: {outcome.archive_path}")
    if outcome.applied:
        print(f"\nrollback with: git revert {outcome.commit_sha}")
    return 0 if outcome.applied else 1


__all__ = ["build_parser", "main"]
