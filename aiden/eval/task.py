"""The eval task model.

A task is: a repo at a known commit, one file whose contents are modified to break something, and a
set of test node ids that must go from failing to passing. Nothing about the *agent* lives here,
which is the "inference and grading are separate halves" seam from `architecture` §12: the task and
its oracle can be handed to any harness, and a harness can be graded without touching the answer key.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Task:
    """One benchmark item, derived from a real repository."""

    id: str
    #: The repository the task was mined from, as a path or URL — for provenance.
    repo: str
    #: Commit the task is derived from. Everything is reproducible from this plus the files below.
    base_commit: str = ""
    #: Path (repo-relative) -> the contents the *agent* starts with. This is the broken state.
    files: dict[str, str] = field(default_factory=dict)
    #: Test node ids that must fail before the run and pass after it. This is the oracle.
    fail_to_pass: list[str] = field(default_factory=list)
    #: Test node ids that must pass both before and after, so a "fix" cannot break the neighbours.
    pass_to_pass: list[str] = field(default_factory=list)
    #: The single file the agent is expected to change — the one that was blanked.
    target_file: str = ""
    #: The function or method the tests exercise, for the prompt and the report.
    target_symbol: str = ""
    #: How the task was made, so a report can say what class of task it is.
    origin: str = "mined"
    notes: str = ""

    # ------------------------------------------------------------------ serialisation

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Task:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> Task:
        return cls.from_dict(json.loads(path.read_text()))

    # ------------------------------------------------------------------ prompt

    def prompt(self) -> str:
        """What the agent is asked. Deliberately says what is observable, not what to do.

        Naming the failing tests rather than the function is the honest framing: the agent is
        expected to read the failure, not to be told the answer by the prompt.
        """
        tests = "\n".join(f"- {node}" for node in self.fail_to_pass)
        return (
            f"In this repository, these tests are failing:\n{tests}\n\n"
            "Find out why and fix it. Run the tests to check your work before you finish, and make "
            "sure you have not broken anything else."
        )

    @property
    def summary(self) -> str:
        symbol = f" ({self.target_symbol})" if self.target_symbol else ""
        return f"{self.id}: {self.target_file}{symbol} · {len(self.fail_to_pass)} test(s)"


def load_tasks(directory: Path) -> list[Task]:
    """Every task in a directory, sorted by id for a stable report order."""
    if not directory.is_dir():
        return []
    return sorted((Task.load(path) for path in directory.glob("*.json")), key=lambda task: task.id)
