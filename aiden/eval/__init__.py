"""Evaluation: mine tasks from a repository, run the agent against them, and gate a change.

The three ideas from `architecture` §12 and `research/05` that shape this package:

- **Inference and grading are separate halves.** `runner` produces a patch; `oracle` decides. The seam
  is what lets two harness configurations be compared without touching the answer key.
- **Tasks are validated, not merely generated.** `mine` only adopts a candidate when the tests can be
  shown to fail without the code and pass with it.
- **`< 3 pp` is noise.** `report.gate` refuses a within-noise improvement, checks cost as well as
  resolved rate, and states the set size next to every rate.
"""

from .oracle import Verdict, grade
from .report import GateDecision, RunReport, TaskResult, gate
from .sandbox import Sandbox, SandboxError
from .task import Task, load_tasks

__all__ = [
    "GateDecision",
    "RunReport",
    "Sandbox",
    "SandboxError",
    "Task",
    "TaskResult",
    "Verdict",
    "gate",
    "grade",
    "load_tasks",
]
