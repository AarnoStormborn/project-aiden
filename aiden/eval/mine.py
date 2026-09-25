"""Mining eval tasks from a repository.

The mechanism: take a function, replace its body with a raise, keep its tests, and then **prove the
task discriminates** — the tests must fail with the body blanked and pass with it restored. A
candidate that cannot be shown to discriminate is discarded, with the reason kept.

That validation step is the entire design. Without it the suite silently fills with tasks that
already pass (so a 100% resolved rate means nothing) or that are unanswerable because the tests were
exercising something else. `research/05`'s argument for private repo-derived tasks only holds if the
tasks are *validated*, not merely generated.

Validation runs inside a disposable worktree, never in the real checkout: a crash mid-validation
would otherwise leave the user's tree broken, which is not an acceptable failure mode for a tool
whose whole job is to be careful.

A note on the class of task this produces: "restore behaviour from tests" is narrower than "diagnose
an unfamiliar failure". The task says so in ``notes`` rather than dressing itself up as SWE-bench.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .sandbox import Sandbox
from .task import Task

#: Bodies shorter than this are single statements, where blanking reduces the task to "write this
#: one expression" and gives the oracle almost nothing to discriminate on.
MIN_BODY_LINES = 2

#: Per-command timeout during mining. A task whose tests take minutes is not usable in a loop.
MINING_TIMEOUT_S = 120

#: Test files that must never serve as an oracle. The eval suite creates sandboxes, so grading it
#: means running an eval inside an eval — self-referential, and the source of a runaway recursion
#: before the sandbox gained its nesting guard.
SELF_REFERENTIAL_TESTS = ("test_eval.py",)

#: A candidate whose tests all depend on one symbol is really "rewrite this module". Blanking
#: `run_loop` breaks 42 tests, which measures a rewrite rather than a fix and gives a resolved rate
#: that says nothing about anything else. Focused tasks are the ones worth having.
MAX_FAIL_TO_PASS = 15


@dataclass(slots=True)
class Candidate:
    """A function that might make a task."""

    symbol: str
    lineno: int
    body_lines: int


@dataclass(slots=True)
class Rejection:
    """Why a candidate was discarded. Kept because a low yield is the interesting result."""

    candidate: str
    reason: str


# --------------------------------------------------------------------------- discovery


def candidate_functions(source: str) -> list[Candidate]:
    """Functions and methods with a body worth blanking."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    found: list[Candidate] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name.startswith("__") and node.name.endswith("__"):
            continue
        body = _statements_without_docstring(node)
        if not body:
            continue
        span = (body[-1].end_lineno or body[-1].lineno) - body[0].lineno + 1
        if span < MIN_BODY_LINES:
            continue
        found.append(Candidate(symbol=node.name, lineno=node.lineno, body_lines=span))
    return found


def _statements_without_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr):
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return body[1:]
    return body


def referencing_tests(tests_dir: Path, symbol: str) -> list[Path]:
    """Test files that mention ``symbol``.

    A name-level check, not coverage: it decides which functions are worth *trying*, and the
    F2P/P2P validation decides whether the result is a task.
    """
    if not tests_dir.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(tests_dir.rglob("test_*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if f".{symbol}(" in text or f"{symbol}(" in text:
            found.append(path)
    return found


def blank_body(source: str, symbol: str) -> str | None:
    """Replace ``symbol``'s body with a raise, keeping its signature and docstring.

    Returns the new source, or ``None`` when the function cannot be rewritten.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.name != symbol:
            continue
        indent = " " * (node.col_offset + 4)
        replacement = f"{indent}raise NotImplementedError('{symbol} was removed for this task')\n"
        body = _statements_without_docstring(node)
        if not body:
            return None
        start = body[0].lineno
        end = node.end_lineno or body[-1].lineno
        lines = source.splitlines(keepends=True)
        return "".join([*lines[: start - 1], replacement, *lines[end:]])
    return None


# --------------------------------------------------------------------------- test running


def failing_tests_from_output(output: str) -> list[str]:
    """Test node ids pytest reported as failing.

    Parsed from pytest's own output because the alternative is a plugin dependency, and the one line
    it reads — ``FAILED path::test [ 42%]`` — is stable.
    """
    found: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("FAILED "):
            continue
        node = stripped[len("FAILED ") :].split(" ")[0].rstrip(":")
        if node and node not in found:
            found.append(node)
    return found


def _pytest_node_ids(relative_tests: list[Path], repo: Path) -> list[str]:
    return [str(path.relative_to(repo)) for path in relative_tests]


# --------------------------------------------------------------------------- mining


def mine(
    repo: Path,
    *,
    modules: list[Path] | None = None,
    tests_dir: Path | None = None,
    base_commit: str = "HEAD",
    limit: int = 10,
    per_module: int = 2,
    max_f2p: int = MAX_FAIL_TO_PASS,
    on_progress=None,
) -> tuple[list[Task], list[Rejection]]:
    """Derive validated tasks from ``repo``.

    ``limit`` caps the yield; ``per_module`` keeps one file from dominating the set, because three
    tasks about the same module measure one thing three times.
    """
    repo = repo.resolve()
    tests_dir = tests_dir or repo / "tests"
    targets = modules or sorted((repo / "aiden").rglob("*.py"))
    tracked = _tracked_files(repo, base_commit)
    tasks: list[Task] = []
    rejections: list[Rejection] = []
    commit = _resolve_commit(repo, base_commit)

    with Sandbox(repo, base_commit=commit) as box:
        for module in targets:
            if len(tasks) >= limit:
                break
            if module.name == "__init__.py" or not module.is_file():
                continue
            relative = module.relative_to(repo)
            # Candidates are found in the working tree but validated in a worktree at the base
            # commit, so a module that is not *committed* cannot be mined: it does not exist in the
            # sandbox. Skipping it with a reason beats a FileNotFoundError from inside validation.
            if tracked and str(relative) not in tracked:
                rejections.append(
                    Rejection(
                        str(relative),
                        "not in the base commit (uncommitted), so it is absent from the sandbox",
                    )
                )
                continue
            try:
                original = module.read_text(encoding="utf-8")
            except OSError:
                continue

            taken = 0
            for candidate in candidate_functions(original):
                if len(tasks) >= limit or taken >= per_module:
                    break
                first = f"{module.stem}--{candidate.symbol}"
                tests = referencing_tests(tests_dir, candidate.symbol)
                if not tests:
                    continue
                if any(path.name in SELF_REFERENTIAL_TESTS for path in tests):
                    rejections.append(
                        Rejection(
                            first,
                            "the only tests that reference it are the eval suite's own, which would "
                            "mean grading an eval with an eval",
                        )
                    )
                    continue

                broken = blank_body(original, candidate.symbol)
                if broken is None:
                    rejections.append(Rejection(first, "the body could not be rewritten"))
                    continue

                node_ids, reason = _validate(box, relative, tests, repo, candidate.symbol)
                if not node_ids:
                    rejections.append(Rejection(first, reason))
                    if on_progress:
                        on_progress(f"rejected {first}: {reason}")
                    continue
                if len(node_ids) > max_f2p:
                    rejections.append(
                        Rejection(
                            first,
                            f"{len(node_ids)} tests depend on it, which is a rewrite rather than a "
                            f"fix (limit {max_f2p})",
                        )
                    )
                    if on_progress:
                        on_progress(f"rejected {first}: {len(node_ids)} tests — too broad")
                    continue

                tasks.append(
                    Task(
                        id=first,
                        repo=str(repo),
                        base_commit=commit,
                        files={str(relative): broken},
                        fail_to_pass=node_ids,
                        target_file=str(relative),
                        target_symbol=candidate.symbol,
                        origin="mined",
                        notes=(
                            "blanked function body; the oracle is the tests that reference it. "
                            "This measures restoring behaviour from tests, not diagnosing an "
                            "unfamiliar failure."
                        ),
                    )
                )
                taken += 1
                if on_progress:
                    on_progress(f"accepted {first}: {len(node_ids)} failing test(s)")

    return tasks, rejections


def _validate(
    box: Sandbox, relative: Path, tests: list[Path], repo: Path, symbol: str
) -> tuple[list[str], str]:
    """Prove the candidate discriminates, inside the sandbox.

    Two halves, both required: blanking must break the tests (otherwise the task resolves for free),
    and the original must pass them (otherwise the oracle disagrees with the baseline and every run
    would be graded unresolved no matter what the agent did).
    """
    module = box.path / relative
    if not module.is_file():
        return [], f"{relative} is absent from the sandbox"
    source = module.read_text(encoding="utf-8")
    blanked = blank_body(source, symbol)
    if blanked is None:
        return [], "the body could not be rewritten"

    nodes = _pytest_node_ids(tests, repo)
    try:
        box.apply({str(relative): blanked})
        result = box.run([box.python(), "-m", "pytest", "-q", "--tb=no", *nodes])
        failing = failing_tests_from_output(result.stdout + result.stderr)
        if not failing:
            return [], (
                "blanking the body broke no test, so the task would resolve without the agent "
                "doing anything"
            )
    finally:
        box.reset()

    restored = box.run([box.python(), "-m", "pytest", "-q", "--tb=no", *failing])
    if not restored.ok:
        tail = (restored.stdout + restored.stderr).strip().splitlines()
        return [], f"the tests fail even unmodified: {tail[-1][:120] if tail else 'no output'}"
    return failing, ""


def _tracked_files(repo: Path, ref: str) -> set[str]:
    """Files present at ``ref``. Used to skip candidates the sandbox will not contain."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            ["git", "ls-tree", "-r", "--name-only", ref],  # noqa: S607 - the user's git
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _resolve_commit(repo: Path, ref: str) -> str:
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            # S607: `git` resolves through PATH on purpose — it must be the user's git, and this
            # repo is developed on machines where the binary moves.
            ["git", "rev-parse", ref],  # noqa: S607
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ref
    return proc.stdout.strip() if proc.returncode == 0 else ref
