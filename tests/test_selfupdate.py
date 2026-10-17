"""Tier-2 self-update: the scope policy, the gates, and the pipeline.

The pipeline tests drive the real `run()` against a real git worktree with an injected evaluator and
approver, so nothing here needs a model. That is deliberate: the parts of this feature that matter
most — that a proposal cannot edit the score function, that a failed lint never reaches the eval,
that a promotion is committed with its evidence — are all testable without inference, and testing
them any other way would mean paying to learn what a fake can tell us for free.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from aiden.approval import ApprovalDecision
from aiden.eval.report import RunReport
from aiden.eval.task import Task
from aiden.selfupdate import (
    EvalSpec,
    Proposal,
    check,
    check_path,
    decide,
    load,
    run,
)
from aiden.selfupdate.gate import Check, StaticGate

# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with the directories the policy cares about."""
    root = tmp_path / "repo"
    (root / "aiden").mkdir(parents=True)
    # A package marker and a mypy target: without one, mypy reports "Missing target module" and the
    # static gate fails for a reason that belongs to the fixture rather than to the pipeline.
    (root / "aiden" / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text(
        '[tool.mypy]\npython_version = "3.12"\nfiles = ["aiden"]\n'
    )
    (root / "aiden" / "config.py").write_text("RESERVE_TOKENS = 16_384\n")
    (root / "aiden" / "loop.py").write_text("def loop():\n    return 1\n")
    (root / "eval" / "tasks").mkdir(parents=True)
    (root / "eval" / "tasks" / "t.json").write_text('{"id": "t"}\n')
    (root / "tests").mkdir()
    (root / "tests" / "test_loop.py").write_text("def test_x():\n    assert True\n")
    (root / ".gitignore").write_text(".aiden/\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=root,
        check=True,
    )
    return root


def proposal_for(repo: Path, **overrides) -> Proposal:
    files = overrides.pop("files", {"aiden/loop.py": "def loop():\n    return 2\n"})
    return Proposal(
        title=overrides.pop("title", "make loop return 2"),
        rationale=overrides.pop("rationale", "the tests want 2"),
        claimed_improvement=overrides.pop("claimed_improvement", "resolves one more task"),
        files=files,
        **overrides,
    )


class RecordingApprover:
    interactive = True

    def __init__(self, approve: bool = True) -> None:
        self.approve = approve
        self.requests: list[dict[str, object]] = []

    async def request(self, *, name, path, diff, sensitive, reason) -> ApprovalDecision:
        self.requests.append(
            {"name": name, "path": path, "diff": diff, "sensitive": sensitive, "reason": reason}
        )
        return ApprovalDecision.yes() if self.approve else ApprovalDecision.no("not today")


def report(
    resolved: int, total: int, cost: float = 0.01, seeds: int = 1, label: str = ""
) -> RunReport:
    from aiden.eval.report import TaskResult

    run = RunReport(label=label, model="fake", harness_version="test")
    for index in range(total):
        run.results.append(
            TaskResult(
                task_id=f"task-{index}",
                seed=1,
                resolved=index < resolved,
                cost_usd=cost,
                turns=3,
            )
        )
    del seeds  # the property is derived from the results
    return run


def evaluator(baseline: RunReport, candidate: RunReport):
    """Mirrors `default_evaluate`, which labels the two halves; the trailer records those labels."""

    async def evaluate(base_commit: str, label: str):
        chosen = baseline if label == "baseline" else candidate
        chosen.label = label
        return chosen

    return evaluate


# --------------------------------------------------------------------------- scope policy


@pytest.mark.parametrize(
    "path",
    [
        "eval/tasks/t.json",
        "eval/oracle.py",
        "aiden/config.py",
        ".aiden/policy/write.toml",
        ".git/config",
        ".venv/lib/site.py",
        "../outside.py",
        "/etc/passwd",
    ],
)
def test_the_policy_refuses_what_it_must(repo: Path, path: str):
    """The score function, the config knobs, and the policy itself are not proposal surfaces."""
    assert check({path: "x"}, repo), f"{path} should have been refused"


def test_the_eval_harness_is_never_writable_because_it_is_the_score_function(repo: Path):
    """Blinding the improver, enforced as code rather than asked for in a prompt.

    A proposal that can edit the tasks or the oracle can optimise the measurement instead of the
    behaviour, and the resulting number is worse than no number because it looks like evidence.
    """
    for path in ("eval/tasks/t.json", "eval/oracle.py", "eval/mine.py", "eval/report.py"):
        violations = check_path(path, repo)
        assert violations, path
        assert "score function" in violations[0].detail


def test_a_proposal_cannot_widen_its_own_permissions(repo: Path):
    """A proposal that can edit the write policy is not gated by it."""
    assert check({".aiden/policy/write.toml": "[allow]\n* = true\n"}, repo)


def test_config_py_is_refused_with_the_reason(repo: Path):
    violations = check_path("aiden/config.py", repo)
    assert violations
    assert "behavioural knobs" in violations[0].detail


def test_existing_tests_are_protected_but_new_ones_are_allowed(repo: Path):
    """The static gate runs pytest, so a proposal must clear the bar, not lower it."""
    assert check_path("tests/test_loop.py", repo), "an existing test file must not be editable"

    new = check_path("tests/test_proposal_added.py", repo)
    assert not new, f"a new test file should be allowed, got {new}"


def test_ordinary_source_changes_are_allowed(repo: Path):
    assert not check_path("aiden/loop.py", repo)
    assert not check_path("aiden/tools/bash.py", repo)
    assert not check_path("docs/plan/selfupdate.md", repo)
    assert not check_path("README.md", repo)


def test_an_empty_proposal_is_refused(repo: Path):
    violations = check({}, repo)
    assert violations and violations[0].rule == "empty"


def test_the_size_bound_is_reviewability_not_correctness(repo: Path):
    """An unreviewably large diff makes approval theatre, which is worse than refusal."""
    files = {f"aiden/mod{index}.py": "x = 1\n" for index in range(30)}
    violations = check(files, repo)
    assert any(v.rule == "too_large" for v in violations)


def test_scope_is_checked_before_any_work_happens(repo: Path):
    """A refused proposal must not create a worktree or spend a cent."""

    async def never_called(base_commit: str, label: str):
        raise AssertionError("the evaluator ran for a proposal the policy refused")

    outcome = asyncio.run(
        run(
            proposal_for(repo, files={"aiden/config.py": "RESERVE_TOKENS = 1\n"}),
            repo=repo,
            evaluator=never_called,
            spec=EvalSpec(tasks=[], seeds=1),
            approver=RecordingApprover(),
        )
    )
    assert outcome.blocked_before_work
    assert outcome.static is None, "no work should have been done"
    assert not outcome.applied


# --------------------------------------------------------------------------- proposal


def test_a_proposal_needs_a_title_and_a_reason(repo: Path):
    with pytest.raises(ValueError, match="title"):
        Proposal(
            title="  ", rationale="because", claimed_improvement="x", files={"aiden/loop.py": ""}
        )
    with pytest.raises(ValueError, match="rationale"):
        Proposal(title="t", rationale=" ", claimed_improvement="x", files={"aiden/loop.py": ""})
    with pytest.raises(ValueError, match="no file changes"):
        Proposal(title="t", rationale="r", claimed_improvement="x", files={})


def test_a_proposal_round_trips_through_json(repo: Path, tmp_path: Path):
    original = proposal_for(repo, evidence=["session abc", "test_loop.py failed"])
    path = original.save(tmp_path / "p.json")
    restored = load(path)
    assert restored.files == original.files
    assert restored.evidence == original.evidence
    assert restored.id == original.id
    assert restored.rationale == original.rationale


def test_a_malformed_proposal_names_the_file(repo: Path, tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        load(bad)
    with pytest.raises(ValueError, match="no proposal at"):
        load(tmp_path / "missing.json")


def test_the_review_diff_is_rendered_from_the_current_contents(repo: Path):
    """The human approves what the file will become, so the diff has to be real."""
    diff = proposal_for(repo).render_diff(repo)
    assert "--- a/aiden/loop.py" in diff and "+++ b/aiden/loop.py" in diff
    assert "-    return 1" in diff and "+    return 2" in diff
    # Line 1 is unchanged, so it appears as context rather than as a change.
    assert " def loop():" in diff


def test_a_deletion_is_shown_as_a_deletion(repo: Path):
    diff = proposal_for(repo, files={"aiden/loop.py": None}).render_diff(repo)
    assert "-def loop():" in diff, "every line of a deleted file is a removal"


def test_the_commit_message_carries_the_eval_trailer(repo: Path):
    """Rollback is a `git log` lookup, so the evidence travels with the commit."""
    message = proposal_for(repo).commit_message(eval_trailer="baseline/2 tasks/3 seeds/+5.0pp")
    assert "Aiden-Eval: baseline/2 tasks/3 seeds/+5.0pp" in message
    assert "Claimed: resolves one more task" in message
    assert "Proposal: proposal-" in message


# --------------------------------------------------------------------------- gates


def test_a_failed_static_gate_never_reaches_the_eval(repo: Path):
    """Failing lint is not a judgement about the idea, and it must not cost money to learn."""
    calls: list[str] = []

    async def evaluate(base_commit: str, label: str):
        calls.append(label)
        return report(0, 1)

    async def scenario():
        # A proposal whose content cannot even be parsed by ruff.
        bad = proposal_for(repo, files={"aiden/loop.py": "def (\n"})
        return await run(
            bad,
            repo=repo,
            evaluator=evaluate,
            spec=EvalSpec(tasks=[Task(id="t", repo="r", files={})], seeds=1),
            approver=RecordingApprover(),
            include_tests=False,
        )

    outcome = asyncio.run(scenario())
    assert outcome.static is not None and not outcome.static.passed
    assert not calls, "the eval ran despite a failed static gate"
    assert outcome.promotion is not None and not outcome.promotion.passed
    assert not outcome.applied


def test_the_static_gate_runs_lint_and_types_in_the_worktree(repo: Path):
    async def scenario():
        return await run(
            proposal_for(repo),
            repo=repo,
            evaluator=None,
            spec=None,
            approver=RecordingApprover(),
            include_tests=False,
        )

    outcome = asyncio.run(scenario())
    names = [check.name for check in outcome.static.checks]  # type: ignore[union-attr]
    assert names == ["ruff", "mypy"], names


def test_an_unavailable_check_is_not_a_failure(repo: Path):
    """A missing tool or a timeout says nothing about the proposal.

    Treating it as a failure would reject valid changes for an environmental reason, and the reason
    would look like a real verdict.
    """
    gate = StaticGate([Check("ruff", ok=False, unavailable=True), Check("mypy", ok=True)])
    assert gate.passed
    assert gate.failures == []
    assert "skip" in gate.render()


def test_the_promotion_decision_delegates_to_the_one_gate_implementation():
    """The criteria live in eval/report.gate(); a second copy would drift from the one that matters."""
    static = StaticGate([Check("ruff", ok=True)])
    better = decide(static, report(1, 4, cost=0.01), report(3, 4, cost=0.01))
    assert better.passed, better.render()

    worse = decide(static, report(3, 4), report(1, 4))
    assert not worse.passed
    assert any("worse" in reason for reason in worse.eval_decision.reasons)  # type: ignore[union-attr]

    pricier = decide(static, report(1, 4, cost=0.01), report(1, 4, cost=0.10))
    assert not pricier.passed, "a 10x cost increase must fail the tolerance"


def test_a_static_failure_reports_that_it_was_not_a_judgement_of_the_idea():
    static = StaticGate([Check("ruff", ok=False, output="E501 line too long")])
    promotion = decide(static, None, None)
    assert not promotion.passed
    assert any("not a judgement about the idea" in reason for reason in promotion.reasons)
    assert "not run" in promotion.render()


def test_the_trailer_records_what_justified_the_commit():
    static = StaticGate([Check("ruff", ok=True)])
    promotion = decide(static, report(1, 4, cost=0.01), report(3, 4, cost=0.01))
    trailer = promotion.trailer(report(3, 4, cost=0.01, label="candidate"))
    assert trailer.startswith("candidate/4 tasks/1 seeds/"), trailer
    assert "pp" in trailer and "cost" in trailer


# --------------------------------------------------------------------------- pipeline


def test_a_promoted_change_is_committed_with_its_eval_trailer(repo: Path):
    """The end-to-end path: worktree, static gate, paired eval, approval, commit."""
    approver = RecordingApprover(approve=True)

    async def scenario():
        return await run(
            proposal_for(repo),
            repo=repo,
            evaluator=evaluator(report(1, 4, cost=0.01), report(4, 4, cost=0.01)),
            spec=EvalSpec(tasks=[Task(id="t", repo="r", files={})], seeds=3),
            approver=approver,
            include_tests=False,
        )

    outcome = asyncio.run(scenario())
    assert outcome.promotion is not None and outcome.promotion.passed, outcome.render()
    assert outcome.applied, outcome.render()

    # The real tree changed, and the commit says what justified it.
    assert (repo / "aiden" / "loop.py").read_text() == "def loop():\n    return 2\n"
    log = subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert "Aiden-Eval: candidate/4 tasks/1 seeds/" in log, log
    assert "selfupdate: make loop return 2" in log

    # And the reviewer was shown the diff, marked sensitive.
    assert approver.requests and approver.requests[0]["sensitive"] is True
    assert "-    return 1" in str(approver.requests[0]["diff"])


def test_a_rejected_proposal_changes_nothing_and_is_still_archived(repo: Path):
    """Rejected-because-measured is knowledge: it is what stops the same idea being re-proposed."""
    before = (repo / "aiden" / "loop.py").read_text()

    async def scenario():
        return await run(
            proposal_for(repo),
            repo=repo,
            evaluator=evaluator(report(4, 4), report(2, 4, cost=0.01)),
            spec=EvalSpec(tasks=[Task(id="t", repo="r", files={})], seeds=1),
            approver=RecordingApprover(),
            include_tests=False,
        )

    outcome = asyncio.run(scenario())
    assert outcome.promotion is not None and not outcome.promotion.passed
    assert not outcome.applied
    assert (repo / "aiden" / "loop.py").read_text() == before, "the tree must be untouched"

    archive = json.loads(outcome.archive_path.read_text())  # type: ignore[union-attr]
    assert archive["passed"] is False
    assert archive["reasons"], "the archive must record why it was rejected"


def test_a_declined_approval_changes_nothing(repo: Path):
    """Approval is the second lock. Declining must leave the tree exactly as it was."""
    before = (repo / "aiden" / "loop.py").read_text()

    async def scenario():
        return await run(
            proposal_for(repo),
            repo=repo,
            evaluator=evaluator(report(1, 4), report(4, 4, cost=0.01)),
            spec=EvalSpec(tasks=[Task(id="t", repo="r", files={})], seeds=1),
            approver=RecordingApprover(approve=False),
            include_tests=False,
        )

    outcome = asyncio.run(scenario())
    assert outcome.declined
    assert not outcome.applied
    assert (repo / "aiden" / "loop.py").read_text() == before
    assert "declined by the human reviewer" in outcome.render()


def test_a_dirty_working_tree_blocks_the_promotion_commit(repo: Path):
    """Otherwise the commit sweeps up unrelated edits and the trailer claims an eval that did not
    cover them — a false record, which is worse than no record."""
    (repo / "aiden" / "unrelated.py").write_text("x = 1\n")  # untracked, uncommitted

    async def scenario():
        return await run(
            proposal_for(repo),
            repo=repo,
            evaluator=evaluator(report(1, 4), report(4, 4, cost=0.01)),
            spec=EvalSpec(tasks=[Task(id="t", repo="r", files={})], seeds=1),
            approver=RecordingApprover(),
            include_tests=False,
        )

    outcome = asyncio.run(scenario())
    assert outcome.promotion is not None and outcome.promotion.passed
    assert not outcome.applied
    assert any("uncommitted changes" in reason for reason in outcome.promotion.reasons)


def test_the_eval_points_the_tasks_at_the_proposal_commit(repo: Path):
    """The mechanism that makes a harness change measurable at all.

    A task's base commit is also the version of the harness that runs inside its worktree, so the
    candidate run re-bases the same tasks onto the proposal's commit. Without this, both halves of
    the comparison would measure the same code and the delta would always be zero.
    """
    seen: list[tuple[str, str]] = []

    async def evaluate(base_commit: str, label: str):
        seen.append((label, base_commit))
        return report(1, 4) if label == "baseline" else report(2, 4)

    async def scenario():
        return await run(
            proposal_for(repo),
            repo=repo,
            evaluator=evaluate,
            spec=EvalSpec(tasks=[Task(id="t", repo="r", files={})], seeds=1),
            approver=RecordingApprover(),
            include_tests=False,
        )

    outcome = asyncio.run(scenario())
    by_label = dict(seen)
    assert by_label["baseline"] == "HEAD"
    assert by_label["candidate"] == outcome.proposal_commit
    assert outcome.proposal_commit and outcome.proposal_commit != "HEAD"
    # And the proposal commit really contains the change, so the eval measured the candidate.
    show = subprocess.run(
        ["git", "show", f"{outcome.proposal_commit}:aiden/loop.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert show == "def loop():\n    return 2\n"


def test_without_an_eval_the_static_gate_alone_decides_and_nothing_promotes(repo: Path):
    """An unmeasured change must not be promoted on the strength of linting alone."""
    approver = RecordingApprover()

    async def scenario():
        return await run(
            proposal_for(repo),
            repo=repo,
            evaluator=None,
            spec=None,
            approver=approver,
            include_tests=False,
        )

    outcome = asyncio.run(scenario())
    assert outcome.static is not None and outcome.static.passed
    assert outcome.promotion is not None and not outcome.promotion.passed
    assert not outcome.applied
    assert not approver.requests, "there was nothing to approve"


def test_the_proposal_worktree_does_not_survive_the_run(repo: Path):
    """Evaluating must not leave checkouts behind: they accumulate and slow every later git call."""

    async def scenario():
        return await run(
            proposal_for(repo),
            repo=repo,
            evaluator=evaluator(report(1, 4), report(1, 4)),
            spec=EvalSpec(tasks=[Task(id="t", repo="r", files={})], seeds=1),
            approver=RecordingApprover(),
            include_tests=False,
        )

    asyncio.run(scenario())
    registered = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert registered.count("worktree ") == 1, registered


def test_a_neutral_change_passes_the_gate_because_the_architecture_says_so(repo: Path):
    """Pinned deliberately: a zero-delta change is promotable, and that is inherited, not accidental.

    Architecture §4.5 promotes when `resolved_delta >= 0` — not `> 0` — so a refactor or a
    pure cost win is allowed through without a capability gain. A self-update that *insists* on a
    positive delta would reject exactly the changes that keep the harness maintainable.

    The consequence a reviewer should know: "promoted" means "not measured as harmful", not
    "measured as better". A change with no gain and no loss can reach the commit, so the diff still
    matters — which is why this pipeline keeps the human approval rather than trusting the number.
    """
    static = StaticGate([Check("ruff", ok=True)])
    neutral = decide(static, report(2, 4, cost=0.01), report(2, 4, cost=0.01))
    assert neutral.passed
    assert neutral.eval_decision is not None
    assert neutral.eval_decision.resolved_delta_pp == 0.0

    # But it still refuses a single new infrastructure failure, which is a real signal.
    from aiden.eval.report import TaskResult

    broken = report(2, 4, cost=0.01)
    broken.results.append(TaskResult(task_id="t", seed=1, resolved=False, infra_failure=True))
    assert not decide(static, report(2, 4, cost=0.01), broken).passed


# --------------------------------------------------------------------------- CLI reachability


def test_the_command_is_registered_in_the_dispatcher():
    """The recurring failure in this repository is machinery that works and is never reached.

    `aiden selfupdate` has to be dispatchable, not merely importable.
    """
    from aiden.cli import USAGE, main

    assert "selfupdate" in USAGE
    # argparse handles `--help` by raising SystemExit(0), so the dispatch is what is asserted here.
    with pytest.raises(SystemExit) as exit_info:
        main(["selfupdate", "--help"])
    assert exit_info.value.code == 0


def test_show_reports_the_policy_verdict_and_writes_nothing(tmp_path: Path, capsys):
    """The reader's first question is 'may this even be proposed', so `show` answers it."""
    from aiden.cli.selfupdate import main

    good = proposal_for(tmp_path, files={"aiden/loop.py": "x = 1\n"})
    path = good.save(tmp_path / "good.json")
    assert main(["show", str(path)]) == 0
    assert "policy: allowed" in capsys.readouterr().out

    bad = proposal_for(tmp_path, files={"eval/oracle.py": "# weakened\n"})
    bad_path = bad.save(tmp_path / "bad.json")
    assert main(["show", str(bad_path)]) == 1, "a refused proposal must not exit zero"
    out = capsys.readouterr().out
    assert "policy: refused" in out
    assert "score function" in out


def test_a_missing_proposal_is_a_usage_error_not_a_traceback(tmp_path: Path, capsys):
    from aiden.cli.selfupdate import main

    assert main(["show", str(tmp_path / "nope.json")]) == 2
    assert "no proposal at" in capsys.readouterr().err


def test_run_refuses_when_there_are_no_tasks(tmp_path: Path, capsys):
    """An unmeasured self-update is not gated, so it must not proceed quietly."""
    from aiden.cli.selfupdate import main

    path = proposal_for(tmp_path).save(tmp_path / "p.json")
    empty = tmp_path / "tasks"
    empty.mkdir()
    assert main(["run", str(path), "--tasks", str(empty)]) == 2
    assert "not gated" in capsys.readouterr().err


# --------------------------------------------------------------------------- which harness runs


def test_the_evaluator_runs_the_harness_at_the_commit_it_is_measuring(repo: Path):
    """The flaw this pins: `default_agent` runs the loop in-process.

    Calling it directly measures the *current checkout* twice and compares the two numbers to each
    other, so a harness proposal reports a zero delta — and since the gate admits a zero delta, it
    would be promoted on a measurement that never ran it. Two things have to hold: the eval runs with
    its working directory inside a checkout of the commit under test, and that commit is passed down
    as `--rebase` so the tasks match the harness.
    """
    from aiden.selfupdate.pipeline import (
        HARNESS_WORKTREE_DIR,
        default_evaluate,
    )

    seen: list[dict[str, object]] = []

    async def fake_run(command, **kwargs):
        seen.append({"command": command, **kwargs})
        # Write a report where the caller will look for it, so the evaluator can return.
        out = Path(command[command.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        RunReport(label="x", model="fake", harness_version="t").save(out)

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return Done()

    import aiden.selfupdate.pipeline as pipeline

    original = pipeline.asyncio.to_thread

    async def to_thread(fn, *args, **kwargs):
        return await fake_run(*args, **kwargs)

    pipeline.asyncio.to_thread = to_thread
    try:
        evaluate = default_evaluate(repo, EvalSpec(tasks=[], seeds=2, model="fake-model"))
        asyncio.run(evaluate("HEAD", "candidate"))
    finally:
        pipeline.asyncio.to_thread = original

    assert seen, "the evaluator never ran anything"
    call = seen[0]
    command = call["command"]
    assert isinstance(command, list)
    # 1. the commit under test is what the tasks are rebased onto
    assert command[command.index("--rebase") + 1] == "HEAD"
    assert command[command.index("--seeds") + 1] == "2"
    assert command[command.index("--model") + 1] == "fake-model"
    # 2. cwd is a checkout of that commit, which is what decides which harness is imported
    cwd = Path(str(call["cwd"]))
    assert HARNESS_WORKTREE_DIR.name in str(cwd), cwd
    assert "aiden" in str(cwd) or cwd.is_dir()
    # 3. the nesting marker is stripped: the child is the grader, and `run_task` re-applies the
    #    marker around the agent inside it
    env = call["env"]
    assert isinstance(env, dict) and "AIDEN_EVAL_ACTIVE" not in env


def test_the_command_it_builds_asks_for_the_right_harness(repo: Path):
    """Asserted separately because none of this is visible in the report it produces."""
    from aiden.selfupdate.pipeline import harness_eval_command

    spec = EvalSpec(tasks=[], seeds=3, model="m", max_turns=7, max_cost_usd=0.5)
    command = harness_eval_command(Path("/t"), Path("/o.json"), "baseline", "abc123", spec)
    assert command[:5] == [command[0], "-m", "aiden", "eval", "run"]
    assert "--rebase" in command and command[command.index("--rebase") + 1] == "abc123"
    assert command[command.index("--label") + 1] == "baseline"
    assert command[command.index("--max-cost") + 1] == "0.5"


def test_a_checkout_really_supplies_its_own_harness(repo: Path):
    """The mechanism the whole comparison rests on, proved without a model.

    Running with cwd inside a checkout puts that checkout's `aiden` package first on the import path,
    so a change in the candidate checkout is the change that runs. If this ever stops holding, the
    self-update gate silently starts measuring the wrong harness — which is why it is asserted here
    and not just assumed from `eval/runner.py`.
    """
    from aiden.eval.sandbox import Sandbox

    prompt = repo / "aiden" / "prompts"
    prompt.mkdir(parents=True, exist_ok=True)
    (prompt / "system.md").write_text("ORIGINAL PROMPT\n")
    (repo / "aiden" / "prompts.py").write_text(
        "from pathlib import Path\n\n\ndef build(cwd):\n"
        "    return (Path(__file__).parent / 'prompts' / 'system.md').read_text()\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "prompt"],
        cwd=repo,
        check=True,
    )

    with Sandbox(repo, base_commit="HEAD", root=repo / ".aiden" / "harness-worktrees") as box:
        (box.path / "aiden" / "prompts" / "system.md").write_text("CANDIDATE PROMPT\n")
        probe = box.run(
            [
                box.python(),
                "-c",
                "from aiden import prompts; from pathlib import Path; print(prompts.build(Path.cwd()))",
            ]
        )
    assert probe.ok, probe.output
    assert "CANDIDATE PROMPT" in probe.stdout, (
        "the checkout's harness did not win: the gate would measure the current checkout instead "
        f"of the candidate. Got: {probe.stdout!r}"
    )


def test_the_selected_tasks_travel_to_the_subprocess(repo: Path):
    """Regression: `--only` bounded nothing, because the task list never reached the child.

    A two-task trial ran six tasks and reported six paired tasks. Nothing failed; it just cost three
    times what it said and measured more than was asked for, which is the quietest way a harness can
    be wrong about its own numbers.
    """
    from aiden.selfupdate.pipeline import harness_eval_command

    spec = EvalSpec(
        tasks=[
            Task(id="alpha", repo="r", files={}),
            Task(id="beta", repo="r", files={}),
        ],
        seeds=1,
    )
    command = harness_eval_command(Path("/t"), Path("/o.json"), "baseline", "abc", spec)
    pairs = [command[index + 1] for index, item in enumerate(command) if item == "--only"]
    assert pairs == ["alpha", "beta"], command


def test_the_eval_reports_are_kept_as_evidence(repo: Path):
    """Regression: `_keep_reports` was written and never called, so the verdict had no evidence.

    The architecture asks a human to approve "diff + eval report". A decision whose numbers were
    discarded is not reviewable, and the archive said `reports: []` while claiming an eval trailer.
    """

    async def scenario():
        return await run(
            proposal_for(repo),
            repo=repo,
            evaluator=evaluator(report(1, 4, cost=0.01), report(2, 4, cost=0.01)),
            spec=EvalSpec(tasks=[Task(id="t", repo="r", files={})], seeds=1),
            approver=RecordingApprover(),
            include_tests=False,
        )

    outcome = asyncio.run(scenario())
    assert len(outcome.report_paths) == 2, outcome.report_paths
    names = {path.name for path in outcome.report_paths}
    assert names == {
        f"{outcome.proposal.id}-baseline.json",
        f"{outcome.proposal.id}-candidate.json",
    }
    for path in outcome.report_paths:
        assert path.exists(), path

    archived = json.loads(outcome.archive_path.read_text())  # type: ignore[union-attr]
    assert len(archived["reports"]) == 2, archived["reports"]
