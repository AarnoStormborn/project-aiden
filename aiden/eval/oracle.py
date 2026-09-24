"""The oracle: whether a run resolved its task.

`architecture` §12's seam lives here. The agent produces a patch; this module runs the task's tests
against it and decides. It knows nothing about how the patch was produced, which is what lets two
harness configurations be compared without touching the answer key.

A task is *resolved* only when every fail-to-pass test passes **and** every pass-to-pass test still
passes. A patch that fixes the target and breaks a neighbour is not a fix, and a runner that scored it
as one would reward exactly the wrong behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .sandbox import Sandbox
from .task import Task

#: Generous: a task's tests are small, but the agent may have left the repo slow to collect.
ORACLE_TIMEOUT_S = 300


@dataclass(slots=True)
class Verdict:
    """The oracle's decision about one run."""

    resolved: bool = False
    f2p_passed: list[str] = field(default_factory=list)
    f2p_failed: list[str] = field(default_factory=list)
    p2p_failed: list[str] = field(default_factory=list)
    #: Tests that were already failing before the run for unrelated reasons. Excluded from the
    #: verdict deliberately: a pre-existing failure is not the agent's doing.
    output: str = ""
    error: str = ""

    @property
    def summary(self) -> str:
        if self.error:
            return f"error: {self.error}"
        return f"f2p {len(self.f2p_passed)}/{len(self.f2p_passed) + len(self.f2p_failed)}" + (
            f" · broke {len(self.p2p_failed)}" if self.p2p_failed else ""
        )


def grade(box: Sandbox, task: Task) -> Verdict:
    """Run the task's tests in the sandbox and decide."""
    node_ids = [*task.fail_to_pass, *task.pass_to_pass]
    if not node_ids:
        return Verdict(error="the task has no tests to grade")

    result = box.run(
        [box.python(), "-m", "pytest", "-q", "--tb=no", *node_ids], timeout=ORACLE_TIMEOUT_S
    )
    output = f"{result.stdout}{result.stderr}"
    if result.returncode == -1 and "timed out" in output:
        return Verdict(error=output.strip())

    from .mine import failing_tests_from_output

    failed = set(failing_tests_from_output(output))
    verdict = Verdict(
        f2p_passed=[node for node in task.fail_to_pass if node not in failed],
        f2p_failed=[node for node in task.fail_to_pass if node in failed],
        p2p_failed=[node for node in task.pass_to_pass if node in failed],
        output=output[-4000:],
    )
    # A missing test is not a passing test: pytest reports collection errors separately, and an
    # agent that deleted the test file must not be scored as having fixed anything.
    if not verdict.f2p_passed and not verdict.f2p_failed and not result.ok:
        verdict.error = "the tests did not run (collection error or missing file)"
        return verdict

    verdict.resolved = not verdict.f2p_failed and not verdict.p2p_failed
    return verdict


def baseline_check(box: Sandbox, task: Task) -> Verdict:
    """Confirm the task is broken before the agent starts.

    Run every time rather than trusted from mining: if the pre-state is already passing, a "resolved"
    verdict from the run that follows would be meaningless.
    """
    return grade(box, task)
