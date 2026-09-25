"""The eval pipeline: mining, sandboxing, grading, reporting, and the gate.

The end-to-end tests use a **fake agent** deliberately. A real one would cost money, vary run to run,
and test the model rather than the harness. A fake that restores the blanked function must grade
`resolved`; a fake that does nothing must grade unresolved. That is the whole contract, and it holds
without a network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiden.eval.mine import (
    base_has_nesting_guard,
    blank_body,
    candidate_functions,
    failing_tests_from_output,
    mine,
    referencing_tests,
)
from aiden.eval.oracle import baseline_check, grade
from aiden.eval.report import GateDecision, RunReport, TaskResult, gate
from aiden.eval.runner import RunOptions, run_task
from aiden.eval.sandbox import CommandResult, Sandbox
from aiden.eval.task import Task, load_tasks
from aiden.loop import LoopResult
from aiden.providers.types import Usage

# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def repo() -> Path:
    """The real repository, since mining is the thing under test."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def simple_task(repo: Path) -> Task:
    """A task mined from the live repo, validated the same way mining validates."""
    tasks, _rejections = mine(repo, limit=1, per_module=1, max_f2p=15)
    assert tasks, "expected at least one minable task in this repo"
    return tasks[0]


# --------------------------------------------------------------------------- candidates


def test_candidate_functions_skips_trivial_bodies():
    source = """
def one_statement():
    return 1

def real(a, b):
    total = a + b
    scaled = total * 2
    return scaled

class Thing:
    def method(self):
        value = self.compute()
        return value + 1
"""
    symbols = {c.symbol for c in candidate_functions(source)}
    assert "real" in symbols
    assert "method" in symbols, "a two-statement body is a legitimate task"
    assert "one_statement" not in symbols, "a single statement gives the oracle nothing"


def test_candidate_functions_skips_dunders_and_stubs():
    source = """
def __init__(self):
    self.a = 1
    self.b = 2

def stub():
    ...
"""
    assert candidate_functions(source) == []


def test_blank_body_keeps_the_signature_and_docstring():
    source = '''def compute(a, b=2, *args):
    """Adds things.

    More detail.
    """
    total = a + b
    return total
'''
    blanked = blank_body(source, "compute")
    assert blanked is not None
    assert "def compute(a, b=2, *args):" in blanked
    assert "Adds things." in blanked, "the docstring is context the agent should keep"
    assert "total = a + b" not in blanked
    assert "raise NotImplementedError" in blanked
    # And it still parses.
    compile(blanked, "x.py", "exec")


def test_blank_body_returns_none_for_an_unknown_symbol():
    assert blank_body("def a():\n    return 1\n", "b") is None


def test_referencing_tests_finds_the_file_that_mentions_a_symbol(tmp_path: Path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_one.py").write_text("from x import alpha\n\ndef test_alpha():\n    alpha()\n")
    (tests / "test_two.py").write_text("def test_other():\n    pass\n")

    found = [p.name for p in referencing_tests(tests, "alpha")]
    assert found == ["test_one.py"]
    assert referencing_tests(tests, "nothing") == []


def test_failing_tests_are_parsed_from_pytest_output():
    output = (
        "tests/test_x.py::test_a PASSED\n"
        "FAILED tests/test_x.py::test_b - AssertionError [ 50%]\n"
        "FAILED tests/test_y.py::test_c - Error\n"
    )
    assert failing_tests_from_output(output) == [
        "tests/test_x.py::test_b",
        "tests/test_y.py::test_c",
    ]


def test_passed_lines_are_not_mistaken_for_failures():
    assert failing_tests_from_output("tests/test_x.py::test_a PASSED\n1 passed") == []


# --------------------------------------------------------------------------- mining


def test_mining_this_repo_yields_validated_tasks(repo: Path):
    tasks, _rejections = mine(repo, limit=3, per_module=1)
    assert tasks, "mining should find at least one task in a repo of this size"
    for task in tasks:
        assert task.fail_to_pass, "an accepted task must have a failing test"
        assert task.base_commit, "a task must record the commit it came from"
        assert task.target_file in task.files, "the starting state must include the target file"
        assert "NotImplementedError" in task.files[task.target_file]


def test_mining_rejects_candidates_that_do_not_discriminate(repo: Path):
    """The validation is the design: a task that resolves for free measures nothing."""
    _tasks, rejections = mine(repo, limit=20, per_module=1)
    assert rejections, "some candidates must be rejected, or validation is not running"
    assert any("broke no test" in r.reason or "too broad" in r.reason for r in rejections)


def test_mining_reports_why_it_rejected(repo: Path):
    _tasks, rejections = mine(repo, limit=2, per_module=1)
    for rejection in rejections:
        assert rejection.reason, "a rejection without a reason is not auditable"


def test_a_mined_task_round_trips_through_disk(tmp_path: Path, simple_task: Task):
    simple_task.save(tmp_path / f"{simple_task.id}.json")
    loaded = load_tasks(tmp_path)[0]
    assert loaded.id == simple_task.id
    assert loaded.files == simple_task.files
    assert loaded.fail_to_pass == simple_task.fail_to_pass


def test_the_task_prompt_names_the_failing_tests(simple_task: Task):
    prompt = simple_task.prompt()
    assert simple_task.fail_to_pass[0] in prompt
    assert "failing" in prompt


# --------------------------------------------------------------------------- sandbox


def test_a_sandbox_is_isolated_from_the_real_tree(repo: Path):
    with Sandbox(repo) as box:
        target = box.path / "aiden" / "diffutil.py"
        original = target.read_text()
        target.write_text("# clobbered\n")
        assert (repo / "aiden" / "diffutil.py").read_text() == original, (
            "a write inside the sandbox must not reach the working tree"
        )


def test_a_sandbox_diff_is_the_patch(repo: Path):
    with Sandbox(repo) as box:
        target = box.path / "aiden" / "diffutil.py"
        target.write_text(target.read_text() + "\n# a change\n")
        diff = box.diff()
        assert "+# a change" in diff
        assert box.changed_files() == ["aiden/diffutil.py"]


def test_reset_discards_the_agent_work(repo: Path):
    with Sandbox(repo) as box:
        (box.path / "aiden" / "diffutil.py").write_text("# clobbered\n")
        box.reset()
        assert "# clobbered" not in (box.path / "aiden" / "diffutil.py").read_text()


def test_two_sandboxes_do_not_share_state(repo: Path):
    with Sandbox(repo) as first, Sandbox(repo) as second:
        assert first.path != second.path
        (first.path / "aiden" / "diffutil.py").write_text("# one\n")
        assert "# one" not in (second.path / "aiden" / "diffutil.py").read_text()


def test_the_sandbox_uses_the_harness_interpreter(repo: Path):
    """The venv carries the test dependencies, and cwd puts the worktree's code first."""
    import sys

    with Sandbox(repo) as box:
        assert box.python() == sys.executable
        which = box.run([box.python(), "-c", "import aiden; print(aiden.__file__)"])
        assert str(box.path) in which.stdout, "the worktree's package must win over the install"


def test_a_sandbox_requires_git(tmp_path: Path):
    from aiden.eval.sandbox import SandboxError

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(SandboxError) as exc:
        Sandbox(plain).create()
    assert "git repository" in str(exc.value)


# --------------------------------------------------------------------------- oracle


def test_the_oracle_says_resolved_when_the_tests_pass(repo: Path, simple_task: Task):
    with Sandbox(repo, base_commit=simple_task.base_commit) as box:
        box.apply(simple_task.files)
        assert grade(box, simple_task).resolved is False, "the task starts broken"
        box.reset()
        assert grade(box, simple_task).resolved is True, "the original code resolves it"


def test_the_baseline_check_detects_an_already_passing_task(repo: Path, simple_task: Task):
    with Sandbox(repo, base_commit=simple_task.base_commit) as box:
        verdict = baseline_check(box, simple_task)
        assert verdict.resolved, (
            "without applying the task files the code is intact, so the pre-check would pass — and "
            "the runner treats that as an infra failure rather than a resolved task"
        )


# --------------------------------------------------------------------------- end to end


def restoring_agent(repo: Path, task: Task):
    """A fake agent that does the work: restores the blanked function.

    Note the source: the base commit, not ``HEAD``. The sandbox's HEAD is the *task* state — it is
    snapshotted so the patch excludes the task's own modification — so restoring from HEAD would only
    bring the blanked version back, and the run would look like an agent that did nothing.
    """

    async def agent(cwd: Path, _prompt: str) -> LoopResult:
        import subprocess

        # ASYNC221: blocking on purpose. The fake agent stands in for a coroutine that does its work
        # in a subprocess (`bash`), and the test asserts on the pipeline, not on concurrency.
        subprocess.run(  # noqa: ASYNC221
            ["git", "checkout", task.base_commit, "--", task.target_file],
            cwd=str(cwd),
            check=False,
            capture_output=True,
        )
        return LoopResult(answer="restored", turns=2, tool_calls=1, usage=Usage(), cost_usd=0.001)

    return agent


def idle_agent():
    async def agent(_cwd: Path, _prompt: str) -> LoopResult:
        return LoopResult(answer="nothing", turns=1, tool_calls=0, usage=Usage(), cost_usd=0.0005)

    return agent


def failing_agent():
    async def agent(_cwd: Path, _prompt: str) -> LoopResult:
        raise RuntimeError("provider exploded")

    return agent


async def test_a_working_agent_grades_resolved(repo: Path, simple_task: Task, tmp_path: Path):
    results = await run_task(
        simple_task,
        agent=restoring_agent(repo, simple_task),
        options=RunOptions(model="fake", seeds=1, sessions_dir=tmp_path / "sessions"),
        worktree_root=tmp_path / "worktrees",
        repo=repo,
    )
    assert len(results) == 1
    result = results[0]
    assert not result.infra_failure, result.error
    assert result.resolved, result.error
    assert result.patch_lines > 0, "a resolved run must have produced a patch"


async def test_an_idle_agent_grades_unresolved(repo: Path, simple_task: Task, tmp_path: Path):
    results = await run_task(
        simple_task,
        agent=idle_agent(),
        options=RunOptions(model="fake", seeds=1, sessions_dir=tmp_path / "sessions"),
        worktree_root=tmp_path / "worktrees",
        repo=repo,
    )
    result = results[0]
    assert not result.infra_failure
    assert not result.resolved
    assert result.patch_lines == 0


async def test_seeds_run_independently(repo: Path, simple_task: Task, tmp_path: Path):
    """Seed 2 must begin from the broken state, not from seed 1's answer."""
    seen: list[int] = []

    async def counting_agent(_cwd: Path, _prompt: str) -> LoopResult:
        seen.append(len(seen) + 1)
        return LoopResult(answer="x", turns=1, tool_calls=0, usage=Usage(), cost_usd=0.0001)

    results = await run_task(
        simple_task,
        agent=counting_agent,
        options=RunOptions(model="fake", seeds=3, sessions_dir=tmp_path / "sessions"),
        worktree_root=tmp_path / "worktrees",
        repo=repo,
    )
    assert [r.seed for r in results] == [1, 2, 3]
    assert seen == [1, 2, 3]


async def test_a_provider_failure_is_an_infra_failure_not_a_miss(
    repo: Path, simple_task: Task, tmp_path: Path
):
    """A harness bug must not be scored as the model failing the task."""
    results = await run_task(
        simple_task,
        agent=failing_agent(),
        options=RunOptions(model="fake", seeds=1, sessions_dir=tmp_path / "sessions"),
        worktree_root=tmp_path / "worktrees",
        repo=repo,
    )
    assert results[0].infra_failure
    assert "provider exploded" in results[0].error


async def test_an_already_passing_task_is_an_infra_failure(repo: Path, tmp_path: Path):
    """If the pre-state passes, a later resolved verdict would mean nothing."""
    from dataclasses import replace

    broken = Task(
        id="broken--task",
        repo=str(repo),
        base_commit=simple_task_commit(repo),
        files={},
        fail_to_pass=["tests/test_command_policy.py::test_read_only_commands_run_unattended"],
    )
    results = await run_task(
        broken,
        agent=idle_agent(),
        options=RunOptions(model="fake", seeds=1, sessions_dir=tmp_path / "sessions"),
        worktree_root=tmp_path / "worktrees",
        repo=repo,
    )
    assert results[0].infra_failure
    assert "already passes" in results[0].error
    _ = replace  # keep the import meaningful


def simple_task_commit(repo: Path) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()


# --------------------------------------------------------------------------- report


def report_with(*results: TaskResult, label: str = "") -> RunReport:
    return RunReport(results=list(results), label=label, model="fake")


def test_the_rate_excludes_infra_failures():
    """A harness that cannot set up a task has not been measured on it."""
    report = report_with(
        TaskResult(task_id="a", resolved=True),
        TaskResult(task_id="b", resolved=False),
        TaskResult(task_id="c", infra_failure=True),
    )
    assert report.resolved_rate == pytest.approx(50.0)
    assert report.summary()["attempted"] == 2


def test_the_report_states_the_set_size_next_to_the_rate():
    """A 100% rate over two tasks invites a misreading that the count prevents."""
    report = report_with(
        TaskResult(task_id="a", resolved=True), TaskResult(task_id="b", resolved=True)
    )
    rendered = report.render()
    assert "100.0% resolved over 2 run(s)" in rendered
    assert "2 task(s)" in rendered
    assert "indicative, not as a result" in rendered


def test_report_round_trips(tmp_path: Path):
    report = report_with(TaskResult(task_id="a", resolved=True, cost_usd=0.01))
    path = tmp_path / "run.json"
    report.save(path)
    loaded = RunReport.load(path)
    assert loaded.results[0].task_id == "a"
    assert loaded.results[0].resolved is True


# --------------------------------------------------------------------------- the gate


def test_the_gate_accepts_a_real_improvement():
    baseline = report_with(
        *[TaskResult(task_id=f"t{i}", resolved=i < 2, cost_usd=0.01) for i in range(5)]
    )
    candidate = report_with(
        *[TaskResult(task_id=f"t{i}", resolved=i < 4, cost_usd=0.01, seed=3) for i in range(5)]
    )
    decision = gate(baseline, candidate)
    assert decision.passed, decision.reasons
    assert decision.resolved_delta_pp == pytest.approx(40.0)


def test_the_gate_rejects_a_within_noise_improvement():
    """One task of fifty is 2 pp, which `research/05` says to treat as noise."""
    baseline = report_with(*[TaskResult(task_id=f"t{i}", resolved=False) for i in range(50)])
    candidate = report_with(
        *[TaskResult(task_id=f"t{i}", resolved=i == 0, seed=3) for i in range(50)]
    )
    decision = gate(baseline, candidate)
    assert not decision.passed
    assert decision.resolved_delta_pp == pytest.approx(2.0)
    assert any("noise floor" in r for r in decision.reasons)


def test_the_gate_rejects_a_cost_blow_up():
    baseline = report_with(
        *[TaskResult(task_id=f"t{i}", resolved=True, cost_usd=0.01) for i in range(5)]
    )
    candidate = report_with(
        *[TaskResult(task_id=f"t{i}", resolved=True, cost_usd=0.05, seed=3) for i in range(5)]
    )
    decision = gate(baseline, candidate)
    assert not decision.passed
    assert any("cost up" in r for r in decision.reasons)


def test_the_gate_rejects_a_regression():
    baseline = report_with(*[TaskResult(task_id=f"t{i}", resolved=True) for i in range(5)])
    candidate = report_with(*[TaskResult(task_id=f"t{i}", resolved=False) for i in range(5)])
    decision = gate(baseline, candidate)
    assert not decision.passed
    assert any("worse" in r for r in decision.reasons)


def test_the_gate_rejects_new_infrastructure_failures():
    baseline = report_with(TaskResult(task_id="a", resolved=True))
    candidate = report_with(
        TaskResult(task_id="a", resolved=True), TaskResult(task_id="b", infra_failure=True)
    )
    decision = gate(baseline, candidate)
    assert not decision.passed
    assert any("infrastructure" in r for r in decision.reasons)


def test_the_gate_refuses_to_compare_different_task_sets():
    baseline = report_with(
        TaskResult(task_id="a", resolved=True), TaskResult(task_id="b", resolved=True)
    )
    candidate = report_with(TaskResult(task_id="c", resolved=True))
    decision = gate(baseline, candidate)
    assert not decision.passed
    assert any("no tasks in common" in r for r in decision.reasons)


def test_a_single_seed_win_is_recorded_but_flagged():
    """A gain over one seed cannot be separated from run-to-run variance."""
    baseline = report_with(*[TaskResult(task_id=f"t{i}", resolved=False) for i in range(10)])
    candidate = report_with(
        *[TaskResult(task_id=f"t{i}", resolved=i < 5, seed=1) for i in range(10)]
    )
    decision = gate(baseline, candidate)
    assert decision.passed
    assert any("seed" in r for r in decision.reasons)


def test_an_identical_run_passes_the_gate():
    """No change is not a regression."""
    results = [TaskResult(task_id=f"t{i}", resolved=True, cost_usd=0.01) for i in range(5)]
    decision = gate(report_with(*results), report_with(*results))
    assert decision.passed, decision.reasons
    assert isinstance(decision, GateDecision)


# ------------------------------------------------- nesting and cleanup (regressions)


def test_a_nested_sandbox_is_refused(repo: Path, monkeypatch):
    """Regression: validation ran the eval suite, which mined again — unbounded recursion.

    One `mine()` call created 3,523 worktrees and took 39 minutes before this guard existed.
    """
    from aiden.eval.sandbox import NESTED_ENV, SandboxError

    monkeypatch.setenv(NESTED_ENV, "1")
    with pytest.raises(SandboxError) as exc:
        Sandbox(repo).create()
    assert "inside a sandbox" in str(exc.value)


def test_a_sandbox_can_opt_into_nesting_for_a_deliberate_case(repo: Path, tmp_path: Path):
    from aiden.eval.sandbox import NESTED_ENV

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(NESTED_ENV, "1")
        box = Sandbox(repo, root=tmp_path / "wt", allow_nested=True)
        box.create()
        try:
            assert box.path.is_dir()
        finally:
            box.remove()


def test_child_processes_are_marked(repo: Path):
    """The guard only works if the marker reaches the tests being graded."""
    with Sandbox(repo) as box:
        result = box.run(
            [box.python(), "-c", "import os; print(os.environ.get('AIDEN_EVAL_ACTIVE'))"]
        )
        assert result.stdout.strip() == "1"


def test_a_sandbox_leaves_nothing_registered(repo: Path, tmp_path: Path):
    """Regression: nothing checked cleanup, so thousands of worktrees leaked."""
    import subprocess

    def registered() -> set[str]:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            line.removeprefix("worktree ")
            for line in proc.stdout.splitlines()
            if line.startswith("worktree ")
        }

    before = registered()
    with Sandbox(repo, root=tmp_path / "wt") as box:
        path = box.path
        assert str(path) in registered(), "the worktree should be registered while in use"
    after = registered()

    assert after == before, f"leaked: {after - before}"
    assert not path.exists(), "the directory should be gone too"


def test_mining_never_uses_the_eval_suite_as_its_own_oracle(repo: Path):
    """A task graded by the eval suite means running an eval inside an eval."""
    tasks, rejections = mine(repo, limit=6, per_module=1)
    for task in tasks:
        for node in task.fail_to_pass:
            assert "test_eval" not in node, f"{task.id} is graded by the eval suite itself"
    assert any("eval suite's own" in r.reason for r in rejections), (
        "candidates referenced only by the eval suite should be rejected with that reason"
    )


def test_mining_is_not_quadratic(repo: Path):
    """Regression: leaked worktrees made every git operation slower, quadrupling runtime.

    Asserted as a worktree count rather than a duration, because a timing assertion would be flaky
    and the count is the actual defect.
    """
    import subprocess

    def count() -> int:
        proc = subprocess.run(
            ["git", "worktree", "list"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        return len(proc.stdout.splitlines())

    before = count()
    mine(repo, limit=2, per_module=1)
    assert count() == before, "mining must not leave worktrees behind"


def _registered_worktrees(repo: Path) -> int:
    import subprocess

    proc = subprocess.run(
        ["git", "worktree", "list"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    return len(proc.stdout.splitlines())


async def test_a_graded_run_cannot_create_sandboxes(repo: Path, simple_task: Task, tmp_path: Path):
    """Regression: the agent runs tests through `bash`, which passes the environment through.

    Without the marker in the runner's *own* environment, a graded run's test suite could mine
    again — one task leaked 333 worktrees because the agent ran `pytest` in the worktree.
    """

    def sandbox_creating_agent():
        async def agent(cwd: Path, _prompt: str) -> LoopResult:
            import subprocess

            # What `bash` would do: run the worktree's own tests, which include the eval suite.
            subprocess.run(  # noqa: ASYNC221 - test helper, blocking on purpose
                [
                    "python",
                    "-c",
                    "import os; from pathlib import Path;print('AIDEN_EVAL_ACTIVE' in os.environ)",
                ],
                cwd=str(cwd),
                capture_output=True,
                check=False,
            )
            return LoopResult(answer="x", turns=1, tool_calls=0, usage=Usage(), cost_usd=0.0001)

        return agent

    import os as _os

    before = _registered_worktrees(repo)
    await run_task(
        simple_task,
        agent=sandbox_creating_agent(),
        options=RunOptions(model="fake", seeds=1, sessions_dir=tmp_path / "sessions"),
        worktree_root=tmp_path / "worktrees",
        repo=repo,
    )
    assert _registered_worktrees(repo) == before, "a graded run leaked worktrees"
    # And the marker is restored rather than left set for the rest of the process.
    assert _os.environ.get("AIDEN_EVAL_ACTIVE") is None


# The eval runner commit, which predates the sandbox nesting guard. Used as a known-unguarded base:
# a task mined from it carries old eval code into its worktree, where nothing refuses to nest.
UNGUARDED_BASE = "1e5ef231"


def test_the_guard_is_detected_in_history(repo: Path):
    """A task's base commit is also the version of the *harness* running inside the worktree.

    Mining freezes both, so a task mined from an unguarded commit reintroduces the worktree leak no
    matter what the current checkout does — the current code is not what executes there.
    """
    assert base_has_nesting_guard(repo, "HEAD") is True
    assert base_has_nesting_guard(repo, UNGUARDED_BASE) is False
    # An unreadable ref is treated as unguarded: the safe assumption keeps the leak out.
    assert base_has_nesting_guard(repo, "0" * 40) is False


def test_mining_refuses_a_base_commit_without_the_nesting_guard(repo: Path):
    """Regression: mining from 1e5ef231 created 169 sandboxes and timed out at 240 s when graded."""
    with pytest.raises(ValueError, match="predates the sandbox nesting guard"):
        mine(repo, base_commit=UNGUARDED_BASE, limit=1)


def test_the_patch_is_visible_even_when_the_agent_commits(repo: Path, simple_task: Task, tmp_path):
    """Regression: `git diff --cached` compares against HEAD, and the agent can move HEAD.

    An agent that commits its own fix made a real change read as an empty patch — a live run reported
    `resolved` with `patch_lines=0`. The diff is now taken against the recorded snapshot commit.
    """
    # `files` is path -> content, the task's starting state.
    (relative, _content), *_rest = simple_task.files.items()
    target = repo / relative
    original = target.read_text()

    with Sandbox(repo, base_commit=simple_task.base_commit, root=tmp_path / "wt") as box:
        box.reset()
        box.apply(simple_task.files)
        box.snapshot("task start")

        edited = box.path / relative
        edited.write_text(edited.read_text() + "\n# agent was here\n")

        assert box.diff_lines() > 0, "an uncommitted edit must show up"
        assert box.changed_files() == [relative]

        box._git("add", "-A")
        box._git(
            "-c",
            "user.name=agent",
            "-c",
            "user.email=agent@localhost",
            "commit",
            "--no-verify",
            "-m",
            "the agent commits its own work",
        )

        assert box.diff_lines() > 0, "a committed fix must still show up in the patch"
        assert box.changed_files() == [relative]

    assert target.read_text() == original, "the real repository must never be touched"


class _DeadSandbox:
    """A sandbox whose directory is gone: every command fails with ENOENT."""

    def python(self) -> str:
        return "python"

    def run(self, _command: list[str], *, timeout: int) -> CommandResult:
        return CommandResult(False, "", "[Errno 2] No such file or directory", -1)


def test_a_run_that_never_ran_is_not_a_pass(simple_task: Task):
    """Regression: grading counted unparsed output as every test passing.

    `f2p_passed` was computed as "not in the parsed failures", so when pytest produced no output at
    all every fail-to-pass test counted as passed and the run scored a perfect result. A live run did
    this while its own sandbox had been deleted underneath it.
    """
    verdict = grade(_DeadSandbox(), simple_task)  # type: ignore[arg-type]
    assert verdict.resolved is False, "a run that never executed must not score as resolved"
    assert "no pytest summary" in verdict.error


def test_a_real_pytest_summary_is_accepted(simple_task: Task):
    """The guard must not reject a genuine run — otherwise it trades a false pass for a false fail."""
    summary = f"{len(simple_task.fail_to_pass)} passed in 0.42s"

    class _PassingBox:
        def python(self) -> str:
            return "python"

        def run(self, _command: list[str], *, timeout: int) -> CommandResult:
            return CommandResult(True, summary, "", 0)

    verdict = grade(_PassingBox(), simple_task)  # type: ignore[arg-type]
    assert verdict.resolved is True


def test_cleanup_cannot_reach_outside_its_own_root(repo: Path, simple_task: Task, tmp_path: Path):
    """Regression: a nested cleanup deleted the live sandbox of the run grading it.

    A graded agent runs this repo's tests, those tests call `run_task`, and the old cleanup swept the
    shared temp root — killing the running sandbox and making the patch read as empty.
    """
    from aiden.eval.runner import prune_worktrees

    live_root = tmp_path / "live"
    with Sandbox(repo, base_commit=simple_task.base_commit, root=live_root) as box:
        assert box.path.exists()
        # A nested run cleaning up its own, unrelated root.
        prune_worktrees(repo, tmp_path / "other-root")
        assert box.path.exists(), "a nested cleanup must not delete a live sandbox"
