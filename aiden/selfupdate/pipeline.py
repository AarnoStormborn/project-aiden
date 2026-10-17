"""Tier-2 self-update: evaluate a proposed change to Aiden, and gate it.

The eight steps from architecture §4.5, with the two the architecture defers left out (tagging and
canary — canary needs real usage over time, so it cannot be built or tested in one sitting):

    1. validate    scope policy: what may be touched at all
    2. worktree    an isolated checkout, so evaluating never mutates the user's tree
    3. static      ruff · mypy · optionally pytest, inside that worktree
    4. eval        paired runs, baseline commit vs proposal commit
    5. decide      eval/report.gate() — the criteria live in exactly one place
    6. approve     required, always: Tier 2 is human-gated by definition
    7. apply       write the files and commit with an Aiden-Eval trailer
    8. archive     keep the proposal and its verdict, including rejections

**Why the eval re-bases the tasks.** A task's base commit is not only the starting state of the code
under test — it is also the version of the *harness* that runs inside the worktree (records/plan
§9a). That is what makes a harness change measurable at all: the baseline run puts the tasks at the
current commit, and the candidate run puts the same tasks at the proposal's commit, so the only
difference between the two numbers is the proposal. It is also why mining refuses a base commit
without the sandbox nesting guard.

**Why step 6 is not configurable.** Tier 2 changes the code that will run every future turn. A
pipeline that could auto-apply it would make the eval the only gate, and research/03 is explicit that
a measurement you can game is not a gate — the harness and its tasks must be outside the improver's
write scope (§Blind the improver), which `scope.py` enforces. The approval is the second lock.

Sources: docs/architecture/aiden-architecture.md §4.5, §12; docs/research/03-continual-learning.md
§Self-update pipeline with guardrails, §Versioning and rollback.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import config
from ..approval import Approver
from ..eval.report import RunReport
from ..eval.runner import RunOptions
from ..eval.sandbox import NESTED_ENV, Sandbox
from ..eval.task import Task
from .gate import Promotion, StaticGate, decide, static_gate
from .proposal import Proposal
from .scope import Violation, check

#: Where proposal worktrees live. Repo-local and gitignored, because a proposal is a checkout of
#: *this* repository and `~/.aiden` holds user state rather than working copies.
PROPOSAL_WORKTREE_DIR = Path(".aiden/worktrees")
#: Where the two harness checkouts live while an eval is running — the baseline commit and the
#: proposal commit. Separate from the proposal worktree, which holds the change being reviewed.
HARNESS_WORKTREE_DIR = Path(".aiden/harness-worktrees")

#: An hour. A nested eval run is a whole eval: several tasks, several seeds, each with its own model
#: turns. This is a ceiling for a hung child, not an expectation.
HARNESS_EVAL_TIMEOUT_S = 3_600
#: Where every proposal and its verdict are kept, rejections included. A rejection is the record of
#: what was measured and did not work, which is the only thing that stops it being re-proposed.
ARCHIVE_DIR = Path(".aiden/proposals")

#: An evaluator: run the eval at ``base_commit`` and return the report. Injected so the pipeline is
#: testable without a model, and so a harness comparison is a matter of supplying a different one.
Evaluator = Callable[[str, str], Awaitable[RunReport]]


@dataclass(slots=True)
class EvalSpec:
    """What to evaluate with. Absent means static-gate-only."""

    tasks: list[Task] = field(default_factory=list)
    seeds: int = 1
    model: str = ""
    max_turns: int = 15
    max_cost_usd: float = 1.0

    def runner_options(self, sessions_dir: Path | None) -> RunOptions:
        return RunOptions(
            model=self.model or config.DEFAULT_MODEL,
            max_turns=self.max_turns,
            max_cost_usd=self.max_cost_usd,
            seeds=self.seeds,
            sessions_dir=sessions_dir,
        )


@dataclass(slots=True)
class Outcome:
    """Everything that happened, in a form a human can read and a test can assert on."""

    proposal: Proposal
    violations: list[Violation] = field(default_factory=list)
    static: StaticGate | None = None
    promotion: Promotion | None = None
    baseline: RunReport | None = None
    candidate: RunReport | None = None
    proposal_commit: str = ""
    applied: bool = False
    commit_sha: str = ""
    declined: bool = False
    archive_path: Path | None = None
    #: Where the two eval reports were kept. The architecture requires a human to review "diff +
    #: eval report", so a verdict whose evidence was thrown away is not reviewable.
    report_paths: list[Path] = field(default_factory=list)

    @property
    def blocked_before_work(self) -> bool:
        return bool(self.violations)

    def render(self) -> str:
        lines = [f"proposal {self.proposal.id}: {self.proposal.title}"]
        if self.violations:
            lines.append("refused by the scope policy (before any work was done):")
            lines.extend(f"  - {violation}" for violation in self.violations)
            lines.append("DO NOT PROMOTE")
            return "\n".join(lines)
        if self.promotion is not None:
            lines.append(self.promotion.render())
        if self.declined:
            lines.append("declined by the human reviewer")
        elif self.applied:
            lines.append(f"applied as {self.commit_sha}")
        return "\n".join(lines)


def trailer_for(outcome: Outcome) -> str:
    if outcome.promotion is None or outcome.candidate is None:
        return "no eval"
    return outcome.promotion.trailer(outcome.candidate)


def default_evaluate(repo: Path, spec: EvalSpec, sessions_dir: Path | None = None) -> Evaluator:
    """The real evaluator: run each half from a checkout of the code being measured.

    **Why this is a subprocess and not an in-process call.** `eval/runner.default_agent` runs the loop
    in the current process, so the harness it exercises is whatever `aiden` package that process
    imported. Calling it directly would measure the *current checkout* twice and compare the results
    to each other — a harness proposal would report a delta of exactly zero, and because the gate
    admits a zero delta, it would be promoted on a measurement that never ran it.

    So each half runs `python -m aiden eval run` with its working directory inside a checkout of the
    commit under test. Running with cwd at a worktree puts that worktree's `aiden` package ahead of
    the installed one, which is the mechanism the eval suite already relies on (and pins in
    `tests/test_eval.py::test_the_worktree_package_wins`).

    `NESTED_ENV` is stripped from the child's environment: the marker means "the code being graded
    must not create sandboxes", and this child is the grader, not the graded. `run_task` re-applies it
    around the agent inside, which is where it belongs.
    """
    tasks_path = (repo / "eval" / "tasks").resolve()

    async def evaluate(base_commit: str, label: str) -> RunReport:
        with Sandbox(repo, base_commit=base_commit, root=repo / HARNESS_WORKTREE_DIR) as harness:
            # Outside the worktree, so the report does not become part of what is being measured.
            output_dir = Path(tempfile.mkdtemp(prefix="aiden-selfupdate-eval-"))
            out = output_dir / f"{label}.json"
            command = harness_eval_command(tasks_path, out, label, base_commit, spec)
            env = {key: value for key, value in os.environ.items() if key != NESTED_ENV}
            # Off the event loop: an eval run is minutes of child process, and blocking the loop for
            # it would stall everything else this process is doing.
            proc = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=str(harness.path),
                env=env,
                capture_output=True,
                text=True,
                timeout=HARNESS_EVAL_TIMEOUT_S,
                check=False,
            )
            if not out.exists():
                raise RuntimeError(
                    f"the {label} eval at {base_commit[:8]} produced no report "
                    f"(exit {proc.returncode}):\n{(proc.stdout + proc.stderr)[-2000:]}"
                )
            return RunReport.load(out)

    return evaluate


def harness_eval_command(
    tasks_path: Path, out: Path, label: str, base_commit: str, spec: EvalSpec
) -> list[str]:
    """The command that evaluates one harness. Separated so it can be asserted on directly.

    The three parts that decide *which code is under test* are the cwd (the caller sets it), the
    `--rebase` commit (which is also the checkout), and the absence of `NESTED_ENV` in the
    environment — none of which are visible in the report, and all of which are one refactor away
    from silently measuring the wrong thing.
    """
    only: list[str] = []
    for task in spec.tasks:
        # The task list has to travel. Without it the subprocess runs whatever the --tasks directory
        # holds, so `--only` bounds nothing and a "two task trial" silently spends six times over —
        # which is how this was found: a two-task demo reported six paired tasks.
        only.extend(["--only", task.id])
    return [
        sys.executable,
        "-m",
        "aiden",
        "eval",
        "run",
        "--tasks",
        str(tasks_path),
        *only,
        "--out",
        str(out),
        "--label",
        label,
        "--rebase",
        base_commit,
        "--seeds",
        str(spec.seeds),
        "--model",
        spec.model or config.DEFAULT_MODEL,
        "--max-turns",
        str(spec.max_turns),
        "--max-cost",
        str(spec.max_cost_usd),
    ]


async def run(
    proposal: Proposal,
    *,
    repo: Path,
    evaluator: Evaluator | None = None,
    spec: EvalSpec | None = None,
    approver: Approver,
    include_tests: bool = True,
    test_target: str = "",
    apply_on_pass: bool = True,
    on_event: Callable[[str], None] | None = None,
) -> Outcome:
    """Take a proposal through the pipeline. Never raises for a refusal; the outcome says why."""
    # One `resolve()` before any await. It is a single stat, and the caller and this function are
    # single-threaded here, so there is no loop to block yet.
    repo = Path(repo).resolve()  # noqa: ASYNC240 - one stat, before the first await
    outcome = Outcome(proposal=proposal)

    def say(message: str) -> None:
        if on_event is not None:
            on_event(message)

    # 1. the scope policy, before any work is done
    outcome.violations = check(proposal.files, repo)
    if outcome.violations:
        say(f"refused by the scope policy: {len(outcome.violations)} violation(s)")
        _archive(repo, outcome)
        return outcome

    # 2. an isolated checkout, so nothing here touches the user's working tree
    root = repo / PROPOSAL_WORKTREE_DIR
    with Sandbox(repo, base_commit=proposal.base_commit, root=root) as box:
        box.apply(
            {
                path: (content or "")
                for path, content in proposal.files.items()
                if content is not None
            }
        )
        for path in proposal.deletes():
            target = box.path / path
            if target.exists():
                target.unlink()

        # Commit the proposal inside the worktree: its sha is what the eval is pointed at, and
        # without a commit there is nothing for a task to be based on.
        box.snapshot(f"selfupdate: {proposal.title}")
        outcome.proposal_commit = box._git("rev-parse", "HEAD").stdout.strip()

        # 3. the static gate
        say("static gate: ruff, mypy" + (", pytest" if include_tests else ""))
        outcome.static = static_gate(box, include_tests=include_tests, test_target=test_target)
        say(
            outcome.static.render().splitlines()[0]
            if outcome.static.checks
            else "static gate: empty"
        )

        # 4-5. the eval gate and the decision
        baseline = candidate = None
        if outcome.static.passed and spec is not None and spec.tasks and evaluator is not None:
            say(f"eval: {len(spec.tasks)} task(s) x {spec.seeds} seed(s), baseline vs candidate")
            baseline = await evaluator(proposal.base_commit, "baseline")
            candidate = await evaluator(outcome.proposal_commit, "candidate")
            outcome.baseline, outcome.candidate = baseline, candidate
        elif outcome.static.passed:
            say("no eval supplied: deciding on the static gate alone")

        outcome.promotion = decide(outcome.static, baseline, candidate)
        say("PROMOTE" if outcome.promotion.passed else "DO NOT PROMOTE")

        # 6. human approval, required
        if outcome.promotion.passed and apply_on_pass:
            approved = await _approve(outcome, approver, repo)
            if approved:
                outcome.applied, outcome.commit_sha = _apply(repo, proposal, outcome)
            else:
                outcome.declined = True

    outcome.report_paths = _keep_reports(repo, outcome)
    outcome.archive_path = _archive(repo, outcome)
    return outcome


async def _approve(outcome: Outcome, approver: Approver, repo: Path) -> bool:
    """Ask, showing the diff. Tier 2 is human-gated by definition, not by configuration."""
    reason = (
        f"{outcome.proposal.title}\n\n"
        f"claimed: {outcome.proposal.claimed_improvement}\n\n"
        f"{trailer_for(outcome)}"
    )
    decision = await approver.request(
        name="selfupdate",
        path=outcome.proposal.id,
        diff=outcome.proposal.render_diff(repo),
        # Always sensitive: a self-update changes the code that runs every later turn, which is the
        # highest blast radius in the project (research/03 §Surfaces).
        sensitive=True,
        reason=reason,
    )
    return decision.approved


def _apply(repo: Path, proposal: Proposal, outcome: Outcome) -> tuple[bool, str]:
    """Write the files into the real tree and commit them with the eval trailer."""
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],  # noqa: S607 - the user's git, via PATH
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    ).stdout.strip()
    if dirty:
        # Refused rather than committed: a commit here would sweep up unrelated edits the user has
        # in flight, and the trailer would then claim an eval report for a change that was not the
        # one evaluated — the commit would be a false record.
        outcome.promotion.reasons.append(  # type: ignore[union-attr]
            "the working tree has uncommitted changes, so the promotion commit would include work "
            "that was not evaluated; commit or stash them first"
        )
        return False, ""

    for path in proposal.deletes():
        target = repo / path
        if target.exists():
            target.unlink()
    for path, content in proposal.files.items():
        if content is None:
            continue
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "add", "-A", "--", *proposal.changed_paths()],  # noqa: S607
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    message = proposal.commit_message(
        eval_trailer=trailer_for(outcome),
        outcome=outcome.promotion.render().splitlines()[-1] if outcome.promotion else "",
    )
    commit = subprocess.run(
        ["git", "commit", "--no-verify", "-F", "-"],  # noqa: S607
        cwd=str(repo),
        input=message,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if commit.returncode != 0:
        return False, ""
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    ).stdout.strip()
    return bool(sha), sha


def _keep_reports(repo: Path, outcome: Outcome) -> list[Path]:
    """Save both eval reports next to the archive, so the verdict has its evidence."""
    directory = repo / ARCHIVE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    kept: list[Path] = []
    for name, run in (("baseline", outcome.baseline), ("candidate", outcome.candidate)):
        if run is None:
            continue
        path = directory / f"{outcome.proposal.id}-{name}.json"
        run.save(path)
        kept.append(path)
    return kept


def _archive(repo: Path, outcome: Outcome) -> Path:
    """Keep the proposal and its verdict. Rejections are kept too — that is the point."""
    directory = repo / ARCHIVE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{outcome.proposal.id}.json"
    record = {
        "proposal": outcome.proposal.to_dict(),
        "recorded_at": datetime.now(UTC).isoformat(),
        "violations": [
            {"path": v.path, "rule": v.rule, "detail": v.detail} for v in outcome.violations
        ],
        "static": (
            None
            if outcome.static is None
            else [
                {"name": c.name, "ok": c.ok, "unavailable": c.unavailable}
                for c in outcome.static.checks
            ]
        ),
        "passed": bool(outcome.promotion and outcome.promotion.passed),
        "reasons": outcome.promotion.reasons if outcome.promotion else [],
        "eval_trailer": trailer_for(outcome),
        "reports": [str(path.name) for path in outcome.report_paths],
        "applied": outcome.applied,
        "commit_sha": outcome.commit_sha,
        "declined": outcome.declined,
    }
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
