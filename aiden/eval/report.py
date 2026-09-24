"""Eval reporting: `resolved @ cost`, seeds, and the change gate.

Three things `research/05` and `architecture` §12 insist on, and each is a rule this module enforces
rather than a convention it hopes for:

- **`< 3 pp` is noise.** Infrastructure alone moved Terminal-Bench 2.0 by 6 pp. The gate refuses a
  within-noise improvement instead of reporting it as a win.
- **Cost is half the metric.** A harness that resolves more by spending ten times as much has not
  improved, so every row carries both and the gate checks both.
- **The set size is stated next to the rate.** A 100% resolved rate over two tasks is not a result,
  and a report that omits the count invites exactly that misreading.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Improvements below this are noise. `research/05`: treat <3 pp as noise until the eval
#: configuration is documented and matched.
NOISE_FLOOR_PP = 3.0

#: How much cost growth is tolerated for a non-negative resolved delta.
COST_TOLERANCE = 0.15


@dataclass(slots=True)
class TaskResult:
    """One task, one seed."""

    task_id: str
    seed: int = 1
    resolved: bool = False
    cost_usd: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    patch_lines: int = 0
    changed_files: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    #: True when the run could not be attempted at all — setup, provider, timeout. Kept separate
    #: from "unresolved" because a harness bug must not look like a model failure.
    infra_failure: bool = False
    error: str = ""
    #: Behavioural signals, so a real effect that pass/fail misses is still visible
    #: (`research/09` records a nanoGPT case where behaviour metrics found what pass/fail did not).
    first_edit_turn: int = 0
    checked_tests: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TaskResult:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass(slots=True)
class RunReport:
    """A whole eval run: many task results, plus what produced them."""

    results: list[TaskResult] = field(default_factory=list)
    model: str = ""
    harness_version: str = ""
    started_at: str = ""
    label: str = ""

    # ------------------------------------------------------------------ aggregation

    def by_task(self) -> dict[str, list[TaskResult]]:
        grouped: dict[str, list[TaskResult]] = {}
        for result in self.results:
            grouped.setdefault(result.task_id, []).append(result)
        return grouped

    @property
    def attempted(self) -> list[TaskResult]:
        return [r for r in self.results if not r.infra_failure]

    @property
    def resolved_rate(self) -> float:
        """Resolved fraction over *attempted* runs, as a percentage.

        Infrastructure failures are excluded from the denominator: a harness that cannot set up a
        task has not been measured on it, and counting those as failures would let a broken runner
        look like a weak model.
        """
        attempted = self.attempted
        if not attempted:
            return 0.0
        return 100.0 * sum(1 for r in attempted if r.resolved) / len(attempted)

    @property
    def mean_cost(self) -> float:
        attempted = self.attempted
        return sum(r.cost_usd for r in attempted) / len(attempted) if attempted else 0.0

    @property
    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.results)

    @property
    def seeds(self) -> int:
        return max((r.seed for r in self.results), default=0)

    def summary(self) -> dict[str, Any]:
        return {
            "tasks": len(self.by_task()),
            "runs": len(self.results),
            "attempted": len(self.attempted),
            "seeds": self.seeds,
            "resolved_rate": round(self.resolved_rate, 1),
            "mean_cost_usd": round(self.mean_cost, 6),
            "total_cost_usd": round(self.total_cost, 6),
            "infra_failures": sum(1 for r in self.results if r.infra_failure),
        }

    def render(self) -> str:
        """A table a human reads, with the set size never separated from the rate."""
        lines: list[str] = []
        header = f"{'task':28} {'seed':>4} {'resolved':>8} {'cost':>9} {'turns':>6} {'patch':>6}"
        lines.append(header)
        lines.append("-" * len(header))
        for task_id, results in sorted(self.by_task().items()):
            for result in sorted(results, key=lambda r: r.seed):
                status = "infra" if result.infra_failure else ("yes" if result.resolved else "no")
                lines.append(
                    f"{task_id:28} {result.seed:>4} {status:>8} "
                    f"${result.cost_usd:>8.4f} {result.turns:>6} {result.patch_lines:>6}"
                )

        stats = self.summary()
        lines.append("")
        lines.append(
            f"{stats['resolved_rate']}% resolved over {stats['attempted']} run(s) of "
            f"{stats['tasks']} task(s) x {stats['seeds']} seed(s)"
        )
        lines.append(
            f"mean ${stats['mean_cost_usd']:.4f} per run · ${stats['total_cost_usd']:.4f} total"
        )
        if stats["infra_failures"]:
            lines.append(f"{stats['infra_failures']} infra failure(s) excluded from the rate")
        if stats["tasks"] < 5:
            lines.append(
                f"note: {stats['tasks']} task(s) is a small set — treat any difference here as "
                "indicative, not as a result"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ persistence

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "label": self.label,
                    "model": self.model,
                    "harness_version": self.harness_version,
                    "started_at": self.started_at,
                    "summary": self.summary(),
                    "results": [r.to_dict() for r in self.results],
                },
                indent=2,
            )
            + "\n"
        )

    @classmethod
    def load(cls, path: Path) -> RunReport:
        raw = json.loads(path.read_text())
        return cls(
            results=[TaskResult.from_dict(r) for r in raw.get("results", [])],
            model=raw.get("model", ""),
            harness_version=raw.get("harness_version", ""),
            started_at=raw.get("started_at", ""),
            label=raw.get("label", ""),
        )


# --------------------------------------------------------------------------- the gate


@dataclass(slots=True)
class GateDecision:
    """Whether a candidate harness may replace a baseline."""

    passed: bool
    reasons: list[str] = field(default_factory=list)
    resolved_delta_pp: float = 0.0
    cost_delta_pct: float = 0.0
    paired_tasks: int = 0
    new_infra_failures: int = 0

    def render(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            f"{verdict}: resolved {self.resolved_delta_pp:+.1f} pp · "
            f"cost {self.cost_delta_pct:+.1f}% · {self.paired_tasks} paired task(s)"
        ]
        for reason in self.reasons:
            lines.append(f"  - {reason}")
        return "\n".join(lines)


def gate(baseline: RunReport, candidate: RunReport) -> GateDecision:
    """Decide whether ``candidate`` may replace ``baseline``.

    The rules, from `architecture` §4.5:

    - resolved delta must be non-negative, **and**
    - cost delta must be within ``COST_TOLERANCE``, **and**
    - no new infrastructure failures, **and**
    - the improvement must clear the noise floor, or be a paired comparison over the same tasks and
      seeds — otherwise "no regression" and "a real gain" are indistinguishable.
    """
    decision = GateDecision(passed=True)
    decision.resolved_delta_pp = candidate.resolved_rate - baseline.resolved_rate
    decision.cost_delta_pct = (
        100.0 * (candidate.mean_cost - baseline.mean_cost) / baseline.mean_cost
        if baseline.mean_cost
        else 0.0
    )

    base_tasks = set(baseline.by_task())
    cand_tasks = set(candidate.by_task())
    decision.paired_tasks = len(base_tasks & cand_tasks)

    decision.new_infra_failures = sum(1 for r in candidate.results if r.infra_failure) - sum(
        1 for r in baseline.results if r.infra_failure
    )

    if decision.resolved_delta_pp < 0:
        decision.passed = False
        decision.reasons.append(f"worse: {decision.resolved_delta_pp:.1f} pp fewer resolved")
    if decision.cost_delta_pct > COST_TOLERANCE * 100:
        decision.passed = False
        decision.reasons.append(
            f"cost up {decision.cost_delta_pct:.1f}%, beyond the {COST_TOLERANCE * 100:.0f}% "
            "tolerance"
        )
    if decision.new_infra_failures > 0:
        decision.passed = False
        decision.reasons.append(f"{decision.new_infra_failures} new infrastructure failure(s)")

    if decision.passed and 0 < decision.resolved_delta_pp < NOISE_FLOOR_PP:
        decision.passed = False
        decision.reasons.append(
            f"{decision.resolved_delta_pp:.1f} pp is inside the noise floor "
            f"({NOISE_FLOOR_PP} pp); it is not evidence of an improvement"
        )
    if decision.passed and decision.paired_tasks == 0:
        decision.passed = False
        decision.reasons.append(
            "no tasks in common between the runs, so the comparison is between different things"
        )
    if decision.passed and candidate.seeds < 3 and decision.resolved_delta_pp > 0:
        decision.reasons.append(
            f"only {candidate.seeds} seed(s): the improvement is recorded, but a single sample "
            "cannot separate it from run-to-run variance"
        )
    return decision


def median_cost(report: RunReport) -> float:
    costs = [r.cost_usd for r in report.attempted]
    return statistics.median(costs) if costs else 0.0
