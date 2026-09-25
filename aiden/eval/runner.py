"""Running the agent against eval tasks.

The agent is injected as a callable, which is what makes the pipeline testable without a model: a
test can supply a fake that restores the blanked function (expect `resolved`) or does nothing (expect
unresolved), and the sandbox, oracle and report are all exercised for real.

Sequence per seed, and the order matters:

1. reset the sandbox and re-apply the task's starting files, so every seed begins byte-identical;
2. **confirm the task is broken before the agent starts** — a pre-state that already passes would
   make any later "resolved" verdict meaningless;
3. run the agent with the worktree as its working directory;
4. capture the patch, then grade it.

Steps 2 and 4 are separate on purpose: 2 checks the *task*, 4 checks the *run*.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config
from ..approval import AllowAll
from ..loop import LoopResult, run_loop
from ..session import SessionStore
from .oracle import baseline_check, grade
from .report import TaskResult
from .sandbox import NESTED_ENV, Sandbox
from .task import Task

#: An agent: given a working directory and a prompt, produce a LoopResult. Injected so tests need no
#: model, and so an eval can compare harnesses by swapping this.
Agent = Callable[[Path, str], Awaitable[LoopResult]]


@dataclass(slots=True)
class RunOptions:
    model: str = ""
    max_turns: int = 15
    max_cost_usd: float = 1.0
    seeds: int = 1
    #: Sessions from an eval run are written here rather than into the user's session directory,
    #: so a benchmark does not fill their history with throwaway worktrees.
    sessions_dir: Path | None = None
    on_event: Callable[[str], None] | None = None


def default_agent(options: RunOptions) -> Agent:
    """The real agent: the harness, pointed at a worktree."""

    async def run(cwd: Path, prompt: str) -> LoopResult:
        from ..providers import ProviderSuite

        suite = ProviderSuite.load()
        store = _session_for(cwd, options)
        try:
            return await run_loop(
                prompt,
                suite=suite,
                model=options.model or config.DEFAULT_MODEL,
                cwd=cwd,
                session=store,
                max_turns=options.max_turns,
                max_cost_usd=options.max_cost_usd,
                # An eval cannot prompt, and it *must* be able to write: the whole task is to change
                # code. `decided_by=flag` is recorded so the transcript does not imply consent.
                approver=AllowAll(decided_by="eval"),
            )
        finally:
            store.close()

    return run


def _session_for(cwd: Path, options: RunOptions) -> SessionStore:
    directory = options.sessions_dir or (config.AIDEN_HOME / "eval-sessions")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{config.new_session_id()}.jsonl"
    store = SessionStore(path, path.stem)
    store.append(
        "session",
        {
            "id": path.stem,
            "cwd": str(cwd),
            "model": options.model or config.DEFAULT_MODEL,
            "harness_version": config.HARNESS_VERSION,
            "kind": "eval",
        },
    )
    return store


def task_prompt(task: Task) -> str:
    return task.prompt()


async def run_task(
    task: Task,
    *,
    agent: Agent,
    options: RunOptions,
    worktree_root: Path | None = None,
    repo: Path | None = None,
) -> list[TaskResult]:
    """Run every seed of one task. Returns one result per seed."""
    repo = (repo or Path.cwd()).resolve()
    results: list[TaskResult] = []

    # The run owns a private root. This is not tidiness: a graded agent runs this repository's own
    # test suite inside its worktree, those tests call `run_task`, and every `run_task` cleans up in a
    # `finally`. With a shared root, a nested cleanup deleted the *running* sandbox — the patch then
    # read as empty and grading degenerated, because every git call in a deleted directory fails.
    owns_root = worktree_root is None
    root = worktree_root or Path(tempfile.mkdtemp(prefix="aiden-eval-run-"))
    try:
        with Sandbox(repo, base_commit=task.base_commit or "HEAD", root=root) as box:
            for seed in range(1, max(1, options.seeds) + 1):
                result = await _run_seed(task, seed, box, agent, options)
                results.append(result)
                if options.on_event:
                    status = (
                        "infra"
                        if result.infra_failure
                        else ("resolved" if result.resolved else "unresolved")
                    )
                    options.on_event(
                        f"  {task.id} seed {seed}: {status} · ${result.cost_usd:.4f} · "
                        f"{result.turns} turns"
                    )
    finally:
        leaked = prune_worktrees(repo, root)
        if leaked and options.on_event:
            # Never silent: a leak is the difference between a 3-minute suite and an 8.5-hour one.
            options.on_event(f"  pruned {leaked} leaked worktree(s)")
        if owns_root:
            shutil.rmtree(root, ignore_errors=True)
    return results


def prune_worktrees(repo: Path, root: Path | None) -> int:
    """Remove sandboxes under ``root`` and detach their registrations. Returns the count removed.

    Only the given root is touched. An earlier version also swept the shared temp root, which turned
    out to be destructive: a graded agent runs this repository's tests, those tests call `run_task`,
    and the nested cleanup therefore deleted the *live* sandbox of the run that was grading it. The
    patch then read as empty and grading degenerate, because every git call in a deleted directory
    fails. Scoping the sweep to the caller's own root makes a nested run unable to reach outward.

    Locked registrations are unlocked first: `git worktree prune` never removes a locked entry, so a
    deleted directory could stay registered forever and the stray count only ever grew.
    """
    removed = 0
    if root is not None and root.exists():
        for directory in root.iterdir():
            if not directory.is_dir():
                continue
            try:
                # The graded run can leave read-only files behind, which makes a plain rmtree fail.
                # `ignore_errors=True` would then hide the failure, which is how this went unnoticed.
                for path in [directory, *directory.rglob("*")]:
                    with contextlib.suppress(OSError):
                        path.chmod(0o700)
                shutil.rmtree(directory)
                removed += 1
            except OSError:
                continue
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        # A locked registration is never removed by `prune` — that is why a run left residue: the
        # directory was gone but the worktree stayed registered, and the next run could not clear it
        # either. Unlock first, then prune. The main worktree is never touched.
        for registration in _sandbox_registrations(repo):
            subprocess.run(  # noqa: S603 - argv list, no shell
                ["git", "worktree", "unlock", registration],  # noqa: S607 - the user's git, via PATH
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        subprocess.run(
            ["git", "worktree", "prune"],  # noqa: S607 - the user's git, via PATH
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    return removed


def _sandbox_registrations(repo: Path) -> list[str]:
    """Paths of every registered worktree except the main one."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],  # noqa: S607 - the user's git, via PATH
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            return []
        paths = [
            line.removeprefix("worktree ")
            for line in proc.stdout.splitlines()
            if line.startswith("worktree ")
        ]
        # The first entry is the repository itself; removing it would be catastrophic.
        return paths[1:]
    return []


async def _run_seed(
    task: Task, seed: int, box: Sandbox, agent: Agent, options: RunOptions
) -> TaskResult:
    result = TaskResult(task_id=task.id, seed=seed)
    started = time.perf_counter()

    try:
        # 1. identical starting state for every seed, recorded as the diff baseline
        box.reset()
        box.apply(task.files)
        box.snapshot(f"task {task.id} seed {seed}")
    except Exception as exc:
        result.infra_failure = True
        result.error = f"could not prepare the sandbox: {type(exc).__name__}: {exc}"
        return result

    # 2. the task must actually be broken before the agent touches it
    pre = baseline_check(box, task)
    if pre.error:
        result.infra_failure = True
        result.error = f"pre-run check failed: {pre.error}"
        return result
    if pre.resolved:
        result.infra_failure = True
        result.error = (
            "the task already passes before the agent runs, so a resolved verdict here would mean "
            "nothing"
        )
        return result

    # 3. run the agent, with the nesting marker set in *this* process.
    #
    # The marker has to be set here rather than only in a sandbox's child environment: the agent runs
    # tests through the `bash` tool, and `bash` passes the environment through to the test process. If
    # it is not set, the graded run's own test suite can create sandboxes — that is how a single task
    # leaked hundreds of worktrees, by running `pytest`, which ran the worktree's `tests/test_eval.py`,
    # which mined again. It wraps only the agent call, because the eval's own sandbox creation is not
    # the code being graded and would be refused by the same guard.
    previous = os.environ.get(NESTED_ENV)
    os.environ[NESTED_ENV] = "1"
    try:
        loop = await agent(box.path, task_prompt(task))
    except Exception as exc:
        result.infra_failure = True
        result.error = f"agent failed: {type(exc).__name__}: {exc}"
        result.duration_s = time.perf_counter() - started
        return result
    finally:
        if previous is None:
            os.environ.pop(NESTED_ENV, None)
        else:
            os.environ[NESTED_ENV] = previous

    result.cost_usd = loop.cost_usd
    result.turns = loop.turns
    result.tool_calls = loop.tool_calls
    result.first_edit_turn = _first_edit_turn(loop)
    result.checked_tests = _checked_tests(loop)

    # 4. grade the patch
    try:
        result.patch_lines = box.diff_lines()
        result.changed_files = box.changed_files()
        verdict = grade(box, task)
    except Exception as exc:
        result.infra_failure = True
        result.error = f"grading failed: {type(exc).__name__}: {exc}"
        result.duration_s = time.perf_counter() - started
        return result

    if verdict.error:
        result.infra_failure = True
        result.error = verdict.error
    else:
        result.resolved = verdict.resolved
        if not verdict.resolved and verdict.f2p_failed:
            result.error = f"still failing: {len(verdict.f2p_failed)} test(s)"

    result.duration_s = time.perf_counter() - started
    return result


# --------------------------------------------------------------------------- behaviour signals


def _first_edit_turn(loop: LoopResult) -> int:
    """The turn of the first mutating tool call, or 0.

    A behavioural signal pass/fail cannot see: an agent that edits on turn 1 and one that reads for
    eight turns can both resolve the task, and they are not the same harness.
    """
    from ..session import ENTRY_TOOL_RESULT, read_entries

    if not loop.session_path:
        return 0
    try:
        entries = list(read_entries(Path(loop.session_path)))
    except OSError:
        return 0
    turn = 0
    for entry in entries:
        if entry.type == "assistant_message":
            turn += 1
        elif entry.type == ENTRY_TOOL_RESULT and entry.payload.get("name") in {"edit", "write"}:
            return turn
    return 0


def _checked_tests(loop: LoopResult) -> bool:
    """Whether the agent ran the tests before finishing.

    `research/09`'s nanoGPT case is the reason this exists: a harness effect that was real and that
    pass/fail missed entirely showed up in behaviour.
    """
    from ..session import ENTRY_TOOL_RESULT, read_entries

    if not loop.session_path:
        return False
    try:
        entries = list(read_entries(Path(loop.session_path)))
    except OSError:
        return False
    for entry in entries:
        if entry.type != ENTRY_TOOL_RESULT:
            continue
        if entry.payload.get("name") != "bash":
            continue
        command = str(entry.payload.get("output", ""))
        if "pytest" in command or "pytest" in str(entry.payload):
            return True
    return False


def describe_options(options: RunOptions) -> dict[str, Any]:
    return {
        "model": options.model,
        "max_turns": options.max_turns,
        "max_cost_usd": options.max_cost_usd,
        "seeds": options.seeds,
    }
