"""What a self-update proposal may touch.

Deny beats allow, and the denials are the interesting part. Two of them exist for reasons that are
not obvious from the path names:

1. **`eval/**` is never writable.** The grading harness, its task definitions and the oracle *are the
   score function*. A proposal that can edit the score function will eventually optimise the score
   function, and the resulting measurement is worse than no measurement because it looks like one.
   This is the blinding constraint from research/03 §"Blind the improver", enforced here as code
   rather than asked for in a prompt — an instruction in a prompt is a request, a refusal in the
   tool layer is a boundary.
2. **`aiden/config.py` is never writable.** The architecture singles it out, and the reason is that
   every constant in it is a behavioural knob: `RESERVE_TOKENS` decides when context is dropped and
   `MAX_TURNS` decides when a run stops. Changing them changes what the harness measures while
   looking like a tuning change. `research/03` puts orchestration and policy at the top of the blast
   radius table for the same reason.

Existing test files are *protected* rather than forbidden: a proposal may add a test, but may not
weaken or delete one. The static gate runs pytest, so being able to edit the tests it runs would let
a proposal pass by lowering the bar instead of clearing it.

Sources: docs/architecture/aiden-architecture.md §4.5 (least privilege per surface, boundaries);
docs/research/03-continual-learning.md §Self-update pipeline with guardrails (surfaces by blast
radius; blind the improver).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: Refused outright, whatever else is true. Exact paths.
NEVER_PATHS: frozenset[str] = frozenset({"aiden/config.py"})

#: Refused outright. Prefixes, matched on path segments.
NEVER_PREFIXES: tuple[str, ...] = (
    # The score function and the grading harness. See rule 1 above.
    "eval/",
    # The write policy itself: a proposal that can widen its own permissions is not gated.
    ".aiden/",
    ".git/",
    # Dependencies change the environment the gate ran in, so the same measurement would no longer
    # describe the same thing. A dependency bump is a human decision.
    ".venv/",
)

#: Roots a proposal may write to, as prefixes.
ALLOWED_PREFIXES: tuple[str, ...] = ("aiden/", "docs/", "scripts/")

#: Individual top-level files a proposal may write to.
ALLOWED_FILES: frozenset[str] = frozenset({"README.md", "AGENTS.md"})

#: Directories where a *new* file is allowed but an *existing* one may not be changed or removed.
PROTECTED_PREFIXES: tuple[str, ...] = ("tests/",)

#: Bound on how much a single proposal may change. Not a correctness rule — a reviewability rule: a
#: diff larger than this cannot be genuinely reviewed, so approving it is theatre.
MAX_FILES_CHANGED = 25
MAX_TOTAL_NEW_LINES = 800


@dataclass(frozen=True, slots=True)
class Violation:
    """One reason a proposal is not allowed to proceed."""

    path: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}: {self.rule} ({self.detail})"


def _normalise(path: str) -> PurePosixPath | None:
    """A repo-relative posix path, or ``None`` if it escapes the repository."""
    candidate = PurePosixPath(path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    if candidate.parts and candidate.parts[0] in ("~",):
        return None
    return candidate


def check_path(path: str, repo: Path) -> list[Violation]:
    """Every reason this one path may not be written by a proposal."""
    violations: list[Violation] = []
    parts = _normalise(path)
    if parts is None or not parts.parts:
        return [Violation(path, "outside_repo", "not a relative path inside the repository")]

    normalised = parts.as_posix()
    existing = (repo / normalised).exists()

    if normalised in NEVER_PATHS:
        violations.append(
            Violation(
                normalised,
                "never",
                "config.py holds behavioural knobs; changing them changes what the gate measures",
            )
        )
        return violations

    for prefix in NEVER_PREFIXES:
        if normalised.startswith(prefix):
            reason = (
                "the eval harness and its tasks are the score function; a proposal that can edit "
                "the score function can optimise the score function"
                if prefix == "eval/"
                else "refused outright by the proposal write policy"
            )
            violations.append(Violation(normalised, "never", f"{reason} [{prefix}]"))
            return violations

    for prefix in PROTECTED_PREFIXES:
        if normalised.startswith(prefix) and existing:
            violations.append(
                Violation(
                    normalised,
                    "protected",
                    "an existing test file may not be edited or deleted; the static gate runs these "
                    "tests, so a proposal must clear the bar rather than lower it. New test files are "
                    "allowed.",
                )
            )
            return violations

    if normalised in ALLOWED_FILES:
        return violations
    if any(normalised.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        return violations
    for prefix in PROTECTED_PREFIXES:
        if normalised.startswith(prefix):
            return violations  # a new test file

    violations.append(
        Violation(
            normalised,
            "not_allowed",
            "outside the writable roots "
            f"({', '.join(ALLOWED_PREFIXES + PROTECTED_PREFIXES)}); anything else is human work, "
            "not a self-update",
        )
    )
    return violations


def check(
    files: dict[str, str | None],
    repo: Path,
    *,
    max_files: int = MAX_FILES_CHANGED,
    max_new_lines: int = MAX_TOTAL_NEW_LINES,
) -> list[Violation]:
    """Every reason this set of changes may not be proposed. Empty means allowed."""
    violations: list[Violation] = []
    if not files:
        return [Violation("(none)", "empty", "a proposal with no changes proposes nothing")]

    for path in sorted(files):
        violations.extend(check_path(path, repo))

    if len(files) > max_files:
        violations.append(
            Violation(
                "(all)",
                "too_large",
                f"{len(files)} files changed, above the {max_files}-file reviewability bound",
            )
        )

    added = sum(
        len((new or "").splitlines())
        for path, new in files.items()
        if new is not None and (repo / path).exists()
    )
    if added > max_new_lines:
        violations.append(
            Violation(
                "(all)",
                "too_large",
                f"{added} lines across existing files, above the {max_new_lines}-line bound",
            )
        )
    return violations
