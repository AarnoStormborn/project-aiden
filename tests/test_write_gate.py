"""The mutation gate inside the loop: checkpoint, approval, and the audit trail.

The important properties: a run with nobody to ask cannot write; a rejection reaches the model with
the reason; a checkpoint exists before the change; and every decision is recorded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiden import config
from aiden.approval import AllowAll, ApprovalDecision, DenyAll
from aiden.checkpoint import CheckpointStore
from aiden.events import ApprovalRequested, ApprovalResolved, RecordingSink
from aiden.loop import run_loop
from aiden.providers.types import Completion, Cost, ModelInfo, ToolCallPart, Usage
from aiden.session import ENTRY_APPROVAL, read_entries


class RecordingApprover:
    """A prompter that answers a fixed verdict and remembers what it was shown."""

    interactive = True

    def __init__(self, approved: bool, note: str = "") -> None:
        self.approved = approved
        self.note = note
        self.requests: list[dict] = []

    async def request(self, **kwargs) -> ApprovalDecision:
        self.requests.append(kwargs)
        return ApprovalDecision(approved=self.approved, note=self.note, decided_by="test")


class FakeSuite:
    """Plays a scripted sequence of tool calls, one per turn, then answers.

    One call per turn because the mutation gate is per-call: a read then an edit is two turns, which
    is also how the model has to do it (an edit is refused unless the file was read first).
    """

    def __init__(self, plan: list[tuple[str, dict]], final: str = "Done."):
        self.plan = plan
        self.final = final
        self.turn = 0
        #: Messages the model was shown, per turn — so a test can assert what it learned.
        self.seen: list[list] = []

    def resolve(self, ref: str) -> ModelInfo:
        return ModelInfo(
            id="fake",
            provider="fake",
            api="anthropic-messages",
            base_url="http://localhost",
            name="Fake",
            cost=Cost(input=1.0, output=1.0),
        )

    async def complete(self, model, messages, tools=None, **kw) -> Completion:
        self.seen.append(list(messages))
        if self.turn < len(self.plan):
            name, arguments = self.plan[self.turn]
            self.turn += 1
            return Completion(
                text=f"Calling {name}.",
                tool_calls=[ToolCallPart(id=f"c{self.turn}", name=name, arguments=arguments)],
                stop_reason="tool_use",
                usage=Usage(input_tokens=10, output_tokens=5),
                cost_usd=0.0001,
            )
        return Completion(
            text=self.final,
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=5),
            cost_usd=0.0001,
        )

    def tool_outputs(self) -> str:
        """Everything the model was told by tools, across all turns."""
        chunks: list[str] = []
        for messages in self.seen:
            for message in messages:
                if getattr(message, "role", "") != "tool":
                    continue
                for part in message.content:
                    chunks.append(str(getattr(part, "output", "")))
        return "\n".join(chunks)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def main():\n    return 1\n")
    return root


@pytest.fixture
def isolated(tmp_path: Path, project: Path, monkeypatch) -> Path:
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config, "SPILL_DIR", tmp_path / "spill")
    monkeypatch.setattr(config, "CHECKPOINT_DIR", tmp_path / "checkpoints")
    return tmp_path


def edit_args(path: str = "src/app.py") -> dict:
    return {"path": path, "old_string": "return 1", "new_string": "return 2"}


def read_then(name: str, arguments: dict, path: str = "src/app.py") -> list[tuple[str, dict]]:
    """The plan the model must use: read, then mutate. Edit refuses an unread file."""
    return [("read", {"path": path}), (name, arguments)]


# --------------------------------------------------------------------------- deny by default


async def test_a_run_with_nobody_to_ask_cannot_write(isolated, project):
    """The single most important property: no interactive approver means no writes."""
    suite = FakeSuite(read_then("edit", edit_args()))
    sink = RecordingSink()

    result = await run_loop(
        "change it", suite=suite, model="fake", cwd=project, sink=sink, approver=DenyAll()
    )

    assert "return 2" not in (project / "src" / "app.py").read_text()
    resolved = sink.of(ApprovalResolved)
    assert resolved and resolved[0].approved is False
    assert result.ok, "a refused write is a normal outcome, not a failed run"


async def test_the_rejection_reaches_the_model_with_the_reason(isolated, project):
    suite = FakeSuite(read_then("edit", edit_args()))
    sink = RecordingSink()

    await run_loop(
        "change it",
        suite=suite,
        model="fake",
        cwd=project,
        sink=sink,
        approver=DenyAll(),
    )

    # The tool result the model sees must explain the refusal and forbid a blind retry.
    from aiden.events import ToolCallFinished

    rejected = [f for f in sink.of(ToolCallFinished) if f.name == "edit"]
    assert rejected and rejected[0].is_error
    assert "declined" in suite.tool_outputs()
    assert "Do not retry" in suite.tool_outputs()


# --------------------------------------------------------------------------- approval


async def test_an_approved_edit_is_applied(isolated, project):
    suite = FakeSuite(read_then("edit", edit_args()))
    sink = RecordingSink()
    approver = RecordingApprover(True)

    await run_loop(
        "change it", suite=suite, model="fake", cwd=project, sink=sink, approver=approver
    )

    assert "return 2" in (project / "src" / "app.py").read_text()


async def test_approval_is_shown_the_diff_not_just_a_tool_name(isolated, project):
    """A prompt that shows only "allow?" trains the user to say yes to nothing."""
    suite = FakeSuite(read_then("edit", edit_args()))
    approver = RecordingApprover(True)

    await run_loop(
        "change it", suite=suite, model="fake", cwd=project, sink=RecordingSink(), approver=approver
    )

    assert approver.requests, "the approver was never asked"
    shown = approver.requests[0]
    assert shown["path"] == "src/app.py"
    assert "-    return 1" in shown["diff"]
    assert "+    return 2" in shown["diff"]


async def test_a_rejected_edit_is_not_applied_and_says_why(isolated, project):
    suite = FakeSuite(read_then("edit", edit_args()))
    sink = RecordingSink()

    await run_loop(
        "change it",
        suite=suite,
        model="fake",
        cwd=project,
        sink=sink,
        approver=RecordingApprover(False, note="not this file"),
    )

    assert "return 1" in (project / "src" / "app.py").read_text()
    # The user's own words reach the model, so it can adapt instead of retrying.
    assert "not this file" in suite.tool_outputs()


async def test_a_refused_change_produces_no_approval_request(isolated, project):
    """No point asking a human about a change the tool is about to reject."""
    suite = FakeSuite(
        read_then(
            "edit", {"path": "src/app.py", "old_string": "zzz not in file", "new_string": "x"}
        )
    )
    approver = RecordingApprover(True)

    await run_loop(
        "change it", suite=suite, model="fake", cwd=project, sink=RecordingSink(), approver=approver
    )

    assert approver.requests == []
    assert "return 1" in (project / "src" / "app.py").read_text()


async def test_a_denied_path_produces_no_approval_request(isolated, project):
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("[core]\n")
    suite = FakeSuite(
        [("read", {"path": ".git/config"}), ("write", {"path": ".git/config", "content": "x"})]
    )
    approver = RecordingApprover(True)

    await run_loop(
        "clobber it",
        suite=suite,
        model="fake",
        cwd=project,
        sink=RecordingSink(),
        approver=approver,
    )

    assert approver.requests == []
    assert (project / ".git" / "config").read_text() == "[core]\n"


# --------------------------------------------------------------------------- checkpoints


async def test_a_checkpoint_is_taken_before_the_change(isolated, project):
    suite = FakeSuite(read_then("edit", edit_args()))
    store = CheckpointStore("s1", base_dir=isolated / "checkpoints", cwd=project)

    await run_loop(
        "change it",
        suite=suite,
        model="fake",
        cwd=project,
        sink=RecordingSink(),
        approver=AllowAll(),
        checkpoints=store,
    )

    latest = store.latest()
    assert latest is not None, "no checkpoint was taken"
    assert "src/app.py" in latest.files
    # And it restores the pre-change state.
    store.restore(latest)
    assert (project / "src" / "app.py").read_text() == "def main():\n    return 1\n"


# --------------------------------------------------------------------------- audit trail


async def test_every_decision_is_logged(isolated, project):
    suite = FakeSuite(read_then("edit", edit_args()))
    result = await run_loop(
        "change it",
        suite=suite,
        model="fake",
        cwd=project,
        sink=RecordingSink(),
        approver=RecordingApprover(True),
    )

    entries = [e for e in read_entries(Path(result.session_path)) if e.type == ENTRY_APPROVAL]
    assert len(entries) == 1
    payload = entries[0].payload
    assert payload["approved"] is True
    assert payload["path"] == "src/app.py"
    assert payload["decided_by"] == "test"
    assert payload["diff_lines"] > 0


async def test_a_declined_decision_is_logged_too(isolated, project):
    suite = FakeSuite(read_then("edit", edit_args()))
    result = await run_loop(
        "change it",
        suite=suite,
        model="fake",
        cwd=project,
        sink=RecordingSink(),
        approver=RecordingApprover(False, note="no"),
    )
    entries = [e for e in read_entries(Path(result.session_path)) if e.type == ENTRY_APPROVAL]
    assert entries[0].payload["approved"] is False


# --------------------------------------------------------------------------- events


async def test_approval_events_are_emitted_around_the_decision(isolated, project):
    suite = FakeSuite(read_then("edit", edit_args()))
    sink = RecordingSink()

    await run_loop(
        "change it", suite=suite, model="fake", cwd=project, sink=sink, approver=AllowAll()
    )

    requested = sink.of(ApprovalRequested)
    resolved = sink.of(ApprovalResolved)
    assert len(requested) == 1 and len(resolved) == 1
    assert requested[0].diff.strip()
    assert resolved[0].decided_by == "flag", "an auto-approval must not look like a user's own"


# --------------------------------------------------------------------------- read-only unaffected


async def test_read_only_tools_need_no_approval(isolated, project):
    suite = FakeSuite([("read", {"path": "src/app.py"})])
    approver = RecordingApprover(True)

    await run_loop(
        "read it", suite=suite, model="fake", cwd=project, sink=RecordingSink(), approver=approver
    )

    assert approver.requests == [], "reading must never prompt"


async def test_writing_a_new_file_is_approved_then_created(isolated, project):
    suite = FakeSuite([("write", {"path": "src/new.py", "content": "x = 1\n"})])
    approver = RecordingApprover(True)

    await run_loop(
        "create it", suite=suite, model="fake", cwd=project, sink=RecordingSink(), approver=approver
    )

    assert (project / "src" / "new.py").read_text() == "x = 1\n"
    assert "+x = 1" in approver.requests[0]["diff"]


async def test_a_checkpoint_restores_a_created_file_by_deleting_it(isolated, project):
    suite = FakeSuite([("write", {"path": "src/new.py", "content": "x = 1\n"})])
    store = CheckpointStore("s2", base_dir=isolated / "checkpoints", cwd=project)

    await run_loop(
        "create it",
        suite=suite,
        model="fake",
        cwd=project,
        sink=RecordingSink(),
        approver=AllowAll(),
        checkpoints=store,
    )

    store.restore(store.latest())
    assert not (project / "src" / "new.py").exists()


async def test_a_declined_change_leaves_no_checkpoint(isolated, project):
    """Capturing the pre-image before the decision leaves empty checkpoints to walk through."""
    suite = FakeSuite(read_then("edit", edit_args()))
    store = CheckpointStore("s3", base_dir=isolated / "checkpoints", cwd=project)

    await run_loop(
        "change it",
        suite=suite,
        model="fake",
        cwd=project,
        sink=RecordingSink(),
        approver=RecordingApprover(False, note="no"),
        checkpoints=store,
    )

    checkpoint = store.latest()
    assert checkpoint is None or not checkpoint.files, "a declined change must not be checkpointed"


async def test_an_approved_change_is_checkpointed(isolated, project):
    suite = FakeSuite(read_then("edit", edit_args()))
    store = CheckpointStore("s4", base_dir=isolated / "checkpoints", cwd=project)

    await run_loop(
        "change it",
        suite=suite,
        model="fake",
        cwd=project,
        sink=RecordingSink(),
        approver=RecordingApprover(True),
        checkpoints=store,
    )

    latest = store.latest()
    assert latest is not None and "src/app.py" in latest.files
