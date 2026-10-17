"""The two gates a self-update must pass: static checks, then the eval.

They answer different questions and both are needed.

**The static gate** asks "is this change even valid" — does it lint, does it typecheck, do the tests
that exist still pass. It is cheap and deterministic, and it catches the large majority of proposals
before any money is spent on inference. Failing it is not a judgement about the idea.

**The eval gate** asks "is this change *better*", which no amount of linting can answer. It is the
expensive half, and it is deliberately not reimplemented here: `eval/report.py:gate()` already
encodes the promotion criteria from architecture §4.5, including the noise floor. Duplicating those
rules would let the two copies drift, and the copy used for self-update is the one that must not be
wrong.

The order matters in the only direction that saves anything: a proposal that fails lint never reaches
the eval, so the common failure costs nothing.

Sources: docs/architecture/aiden-architecture.md §4.5 (steps 3-6); docs/research/03-continual-
learning.md §The gate; aiden/eval/report.py (the criteria themselves).
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field

from ..eval.report import GateDecision, RunReport
from ..eval.report import gate as eval_gate
from ..eval.sandbox import Sandbox

#: Static-check budgets. Generous for lint on a large tree; the eval gate is what bounds the run.
LINT_TIMEOUT_S = 180
TYPECHECK_TIMEOUT_S = 300
TEST_TIMEOUT_S = 1_200


@dataclass(slots=True)
class Check:
    """One static check's outcome."""

    name: str
    ok: bool
    output: str = ""
    duration_s: float = 0.0
    #: True when the check could not run at all (missing tool, timeout). Distinct from failing: a
    #: crashed checker says nothing about the proposal, and treating it as "fail" would silently
    #: reject valid changes for an environmental reason.
    unavailable: bool = False

    def render(self) -> str:
        state = "skip" if self.unavailable else ("ok" if self.ok else "FAIL")
        return f"{self.name}: {state} ({self.duration_s:.1f}s)"


@dataclass(slots=True)
class StaticGate:
    """The static checks, in the order they run."""

    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.ok or check.unavailable for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok and not check.unavailable]

    def render(self) -> str:
        lines = [check.render() for check in self.checks]
        for check in self.failures:
            # The tail of the output, which is where linters and type checkers put the error.
            tail = check.output.strip().splitlines()[-8:]
            lines.extend(f"    {line}" for line in tail)
        return "\n".join(lines)


def _run(box: Sandbox, name: str, command: list[str], timeout: int) -> Check:
    started = time.perf_counter()
    result = box.run(command, timeout=timeout)
    duration = time.perf_counter() - started
    output = f"{result.stdout}{result.stderr}"
    # A timeout or a missing executable is environmental, not a verdict on the change.
    unavailable = result.returncode == -1
    return Check(name, result.ok, output, duration, unavailable=unavailable)


def static_gate(
    box: Sandbox,
    *,
    include_tests: bool = True,
    test_target: str = "",
) -> StaticGate:
    """Lint, typecheck, and optionally test the proposal inside its worktree.

    ``include_tests`` exists because the full suite takes minutes: a caller iterating on a proposal
    wants the fast answer first, and the eval gate is the slower and more meaningful one anyway.
    """
    checks = [
        _run(box, "ruff", [box.python(), "-m", "ruff", "check", "."], LINT_TIMEOUT_S),
        _run(box, "mypy", [box.python(), "-m", "mypy"], TYPECHECK_TIMEOUT_S),
    ]
    if include_tests:
        target = shlex.split(test_target) if test_target else []
        checks.append(
            _run(
                box,
                "pytest",
                [box.python(), "-m", "pytest", "-q", *target],
                TEST_TIMEOUT_S,
            )
        )
    return StaticGate(checks)


@dataclass(slots=True)
class Promotion:
    """The full verdict: static gate, eval gate, and whether the change may be applied."""

    static: StaticGate
    eval_decision: GateDecision | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        if not self.static.passed:
            return False
        if self.eval_decision is None:
            return False
        return self.eval_decision.passed

    def render(self) -> str:
        lines = ["static gate:", *(f"  {line}" for line in self.static.render().splitlines())]
        lines.append("eval gate:")
        if self.eval_decision is not None:
            lines.extend(f"  {line}" for line in self.eval_decision.render().splitlines())
        elif not self.static.passed:
            lines.append("  not run (the static gate did not pass)")
        else:
            # Distinguishing this from the line above matters: "the eval did not run because the
            # change is broken" and "no eval was supplied" are opposite situations, and the second
            # one means nothing was measured at all.
            lines.append("  not supplied — nothing was measured")
        if self.reasons:
            lines.append("decision:")
            lines.extend(f"  - {reason}" for reason in self.reasons)
        lines.append(f"{'PROMOTE' if self.passed else 'DO NOT PROMOTE'}")
        return "\n".join(lines)

    def trailer(self, candidate: RunReport) -> str:
        """The `Aiden-Eval` trailer: what produced this verdict, so the commit is reproducible.

        A commit without it is a self-modification with no record of what justified it, and the
        architecture requires every eval run to be traceable to a state.
        """
        if self.eval_decision is None:
            return "not run (static gate failed)"
        return (
            f"{candidate.label or 'unlabelled'}/"
            f"{len(candidate.by_task())} tasks/{candidate.seeds} seeds/"
            f"{self.eval_decision.resolved_delta_pp:+.1f}pp/"
            f"{self.eval_decision.cost_delta_pct:+.1f}%cost"
        )


def decide(
    static: StaticGate,
    baseline: RunReport | None,
    candidate: RunReport | None,
) -> Promotion:
    """Combine the two gates into one promotion decision.

    ``baseline`` and ``candidate`` are ``None`` when the static gate failed: the eval is not run, so
    there is nothing to decide and no verdict to imply.
    """
    promotion = Promotion(static=static)
    if not static.passed:
        promotion.reasons.append(
            "the static gate failed, so the eval was not run — this is not a judgement about the "
            "idea, only about whether the change is well-formed"
        )
        return promotion
    if baseline is None or candidate is None:
        promotion.reasons.append("the static gate passed but no eval report was supplied")
        return promotion

    promotion.eval_decision = eval_gate(baseline, candidate)
    if not promotion.eval_decision.passed:
        promotion.reasons.append(
            "the eval gate rejected the change; the reasons above are the record of why"
        )
    return promotion
