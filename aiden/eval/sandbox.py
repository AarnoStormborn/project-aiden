"""Disposable git worktrees, one per eval task.

Three reasons a worktree rather than running in place:

- **The agent's writes cannot reach the real working tree.** An eval run is an untrusted coding
  agent; pointing it at the user's checkout and hoping is not a plan.
- **Every seed starts byte-identical.** Comparing seeds or harness configurations means nothing if
  the inputs drifted.
- **`git diff` *is* the patch.** No bespoke patch-capture, and the report can show exactly what the
  agent produced.

Validation during mining uses the same mechanism, so a half-written candidate never touches the real
repository — a run that crashed mid-validation would otherwise leave the user's tree broken.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

WORKTREE_TIMEOUT_S = 120

#: Set for every process the sandbox starts. A sandbox nested inside a sandbox means the tests being
#: graded are themselves running an eval, which is how a single `mine()` call once created 3,500
#: worktrees and took 39 minutes: validation ran `tests/test_eval.py`, which called `mine()`, which
#: validated by running `tests/test_eval.py` again.
NESTED_ENV = "AIDEN_EVAL_ACTIVE"


@dataclass(slots=True)
class CommandResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int

    @property
    def output(self) -> str:
        return f"{self.stdout}{self.stderr}"


class SandboxError(RuntimeError):
    """The sandbox could not be created. Carries a reason a human can act on."""


class Sandbox:
    """A git worktree at a fixed commit, removed when the context exits."""

    def __init__(
        self,
        repo: Path,
        *,
        base_commit: str = "HEAD",
        root: Path | None = None,
        allow_nested: bool = False,
    ) -> None:
        self.repo = repo.resolve()
        self.base_commit = base_commit
        self._root = Path(root or Path(tempfile.gettempdir()) / "aiden-eval-worktrees")
        self._path: Path | None = None
        #: The commit recorded by ``snapshot()``. Diffs are taken against this, never `HEAD`, because
        #: `HEAD` moves when the agent commits its own work.
        self._snapshot_sha: str | None = None
        #: Only a test that knows it is grading a sandbox-creating test should set this.
        self.allow_nested = allow_nested

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> Sandbox:
        self.create()
        return self

    def __exit__(self, *exc: object) -> None:
        self.remove()

    def create(self) -> Path:
        if self._path is not None:
            return self._path
        # Structural guard, independent of environment inheritance. A sandbox is never created from
        # a *task snapshot*: that commit exists only inside a sandbox, so seeing it as the base means
        # sandboxes are being made inside a sandbox. This is what catches the graded code's own test
        # suite, which runs through paths (`uv run` re-executing python, plugin-spawned subprocesses)
        # that do not reliably carry an environment variable.
        if not self.allow_nested and _is_task_snapshot(self.repo, self.base_commit):
            raise SandboxError(
                f"refused: {self.base_commit[:8]} is a task snapshot, which only exists inside a "
                "sandbox — so this would nest an eval inside an eval. Pass allow_nested=True if that "
                "is genuinely intended."
            )
        if os.environ.get(NESTED_ENV) and not self.allow_nested:
            raise SandboxError(
                "refused to create a sandbox inside a sandbox. The code under test is itself "
                "running an eval, so its tests would recurse without bound. If a task genuinely "
                "needs to grade a test that creates sandboxes, pass allow_nested=True."
            )
        if not (self.repo / ".git").exists():
            raise SandboxError(
                f"{self.repo} is not a git repository; eval tasks need one so the patch is "
                "recoverable and seeds start from identical inputs."
            )
        self._root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="task-", dir=self._root))

        result = self._git(
            "worktree", "add", "--detach", "--force", str(directory), self.base_commit
        )
        if not result.ok:
            raise SandboxError(f"could not create a worktree: {result.output.strip()[:400]}")
        self._path = directory
        return directory

    def remove(self) -> None:
        if self._path is None:
            return
        self._git("worktree", "remove", "--force", str(self._path))
        self._path = None

    @property
    def path(self) -> Path:
        if self._path is None:
            raise SandboxError("the sandbox has not been created")
        return self._path

    # ------------------------------------------------------------------ contents

    def apply(self, files: dict[str, str]) -> None:
        """Write the task's starting files into the worktree."""
        for relative, content in files.items():
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def snapshot(self, message: str = "task start") -> None:
        """Commit the current contents so a later ``diff()`` shows only what the agent changed.

        Without this the patch included the task's own modification — the blanked function appeared
        as part of the agent's answer, inflating every patch-size number and making a no-op agent look
        like it had done something.
        """
        if self._git("status", "--porcelain").stdout.strip():
            self._git("add", "-A")
            self._git(
                "-c",
                "user.name=aiden-eval",
                "-c",
                "user.email=eval@localhost",
                "commit",
                "--no-verify",
                "--allow-empty",
                "-m",
                message,
            )
        # Recorded even when there was nothing to commit, so the diff base is always the task start
        # rather than whatever the agent left behind.
        self._snapshot_sha = self._git("rev-parse", "HEAD").stdout.strip() or None

    def reset(self) -> None:
        """Discard everything the agent did, keeping the task's starting files.

        Used between seeds: the next seed must begin from the same broken state, not from the
        previous seed's answer.
        """
        self._git("checkout", "--", ".")
        self._git("clean", "-fd")

    def diff(self) -> str:
        """The patch the agent produced, including new files.

        Diffed against the snapshot commit, not `HEAD`: an agent that commits its own fix moves `HEAD`
        onto that fix, and `git diff --cached` then reports an empty patch. Measured before the fix:
        edit + commit gave 0 lines, the identical edit without a commit gave 2, and a live run
        reported `resolved` with a patch of 0 — a real fix scored as no change at all.
        """
        self._git("add", "-A")
        result = self._git("diff", "--cached", "--no-color", *self._diff_base())
        return result.stdout

    def changed_files(self) -> list[str]:
        result = self._git("diff", "--cached", "--name-only", *self._diff_base())
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _diff_base(self) -> list[str]:
        """The ref to diff against: the recorded task-start commit, falling back to `HEAD`."""
        return [self._snapshot_sha] if self._snapshot_sha else []

    def diff_lines(self) -> int:
        """Added plus removed lines, for the report's patch-size column."""
        total = 0
        for line in self.diff().splitlines():
            if line.startswith(("+++", "---")):
                continue
            if line.startswith(("+", "-")):
                total += 1
        return total

    # ------------------------------------------------------------------ running

    def run(self, command: list[str], *, timeout: int = WORKTREE_TIMEOUT_S) -> CommandResult:
        """Run a command in the worktree, with the harness's own interpreter.

        ``sys.executable`` matters: the venv carries the test dependencies, and running with cwd set
        to the worktree puts its copy of the package ahead of the editable install, so the code under
        test is the worktree's.
        """
        env = dict(os.environ)
        env[NESTED_ENV] = "1"
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, no shell
                command,
                cwd=str(self.path),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(False, "", f"timed out after {timeout}s", -1)
        except OSError as exc:
            return CommandResult(False, "", f"could not run {command[0]}: {exc}", -1)
        return CommandResult(
            proc.returncode == 0, proc.stdout or "", proc.stderr or "", proc.returncode
        )

    def python(self) -> str:
        """The interpreter tests should run under."""
        return sys.executable

    # ------------------------------------------------------------------ internals

    def _git(self, *args: str) -> CommandResult:
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, no shell
                ["git", *args],  # noqa: S607 - the user's git, resolved through PATH
                cwd=str(self.repo if args and args[0] == "worktree" else self.path),
                capture_output=True,
                text=True,
                timeout=WORKTREE_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(False, "", str(exc), -1)
        return CommandResult(
            proc.returncode == 0, proc.stdout or "", proc.stderr or "", proc.returncode
        )


def _is_task_snapshot(repo: Path, ref: str) -> bool:
    """Whether ``ref`` is a commit this module created inside a sandbox."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            ["git", "log", "-1", "--format=%s", ref],  # noqa: S607 - the user's git
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip().startswith("task ")


def cleanup_worktrees(repo: Path) -> int:
    """Remove worktrees left behind by a crashed run. Returns how many were pruned."""
    try:
        proc = subprocess.run(
            ["git", "worktree", "prune"],  # noqa: S607 - the user's git, resolved through PATH
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=WORKTREE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return 0 if proc.returncode != 0 else 1
