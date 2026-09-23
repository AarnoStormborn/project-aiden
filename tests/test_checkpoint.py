"""Checkpoints: capture per turn, restore precisely, and never write outside the project."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiden.checkpoint import CheckpointStore


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("original\n")
    return root


@pytest.fixture
def store(tmp_path: Path, project: Path) -> CheckpointStore:
    return CheckpointStore("sess1", base_dir=tmp_path / "checkpoints", cwd=project)


def test_capture_then_restore_undoes_a_modification(store: CheckpointStore, project: Path):
    checkpoint = store.begin(1)
    store.capture(checkpoint, project / "src" / "app.py")
    (project / "src" / "app.py").write_text("changed\n")

    changed = store.restore(checkpoint)

    assert changed == ["src/app.py"]
    assert (project / "src" / "app.py").read_text() == "original\n"


def test_restore_deletes_a_file_that_did_not_exist(store: CheckpointStore, project: Path):
    """A created file must be removed, not restored to empty."""
    checkpoint = store.begin(1)
    store.capture(checkpoint, project / "src" / "new.py")
    (project / "src" / "new.py").write_text("brand new\n")

    changed = store.restore(checkpoint)

    assert changed == ["src/new.py"]
    assert not (project / "src" / "new.py").exists()


def test_capture_is_idempotent_within_a_turn(store: CheckpointStore, project: Path):
    """The pre-image must be the state *before the turn*, not before the latest edit."""
    checkpoint = store.begin(1)
    store.capture(checkpoint, project / "src" / "app.py")
    (project / "src" / "app.py").write_text("first edit\n")
    store.capture(checkpoint, project / "src" / "app.py")  # must NOT re-capture
    (project / "src" / "app.py").write_text("second edit\n")

    store.restore(checkpoint)

    assert (project / "src" / "app.py").read_text() == "original\n"


def test_separate_turns_have_separate_checkpoints(store: CheckpointStore, project: Path):
    first = store.begin(1)
    store.capture(first, project / "src" / "app.py")
    (project / "src" / "app.py").write_text("after turn 1\n")

    second = store.begin(2)
    store.capture(second, project / "src" / "app.py")
    (project / "src" / "app.py").write_text("after turn 2\n")

    store.restore(second)
    assert (project / "src" / "app.py").read_text() == "after turn 1\n"
    store.restore(first)
    assert (project / "src" / "app.py").read_text() == "original\n"


def test_latest_returns_the_highest_turn(store: CheckpointStore):
    store.begin(1)
    store.begin(2)
    store.begin(3)
    latest = store.latest()
    assert latest is not None and latest.turn == 3


def test_existing_is_newest_first(store: CheckpointStore):
    for turn in (1, 2, 3):
        store.begin(turn)
    assert [c.turn for c in store.existing()] == [3, 2, 1]


def test_empty_store_has_no_latest(store: CheckpointStore):
    assert store.latest() is None
    assert store.existing() == []


def test_capture_refuses_a_path_outside_the_project(store: CheckpointStore, tmp_path: Path):
    """A checkpoint outside the tree would let undo write wherever it liked."""
    outside = tmp_path / "elsewhere.py"
    outside.write_text("secret\n")
    checkpoint = store.begin(1)
    assert store.capture(checkpoint, outside) is None


def test_restore_ignores_a_tampered_manifest_pointing_outside(
    store: CheckpointStore, project: Path, tmp_path: Path
):
    """A hand-edited manifest must not become an arbitrary-write primitive."""
    outside = tmp_path / "victim.txt"
    outside.write_text("do not touch\n")

    checkpoint = store.begin(1)
    store.capture(checkpoint, project / "src" / "app.py")
    manifest = checkpoint.manifest_path
    import json

    raw = json.loads(manifest.read_text())
    raw["files"]["../victim.txt"] = {"backup": "", "sha256": "", "existed": False}
    manifest.write_text(json.dumps(raw))

    store.restore(checkpoint)

    assert outside.read_text() == "do not touch\n", "restore must stay inside the project"


def test_a_manifest_survives_a_reload(store: CheckpointStore, project: Path):
    checkpoint = store.begin(1)
    store.capture(checkpoint, project / "src" / "app.py")
    (project / "src" / "app.py").write_text("changed\n")

    reloaded = CheckpointStore("sess1", base_dir=store.root.parent, cwd=project).latest()
    assert reloaded is not None
    assert "src/app.py" in reloaded.files
    CheckpointStore("sess1", base_dir=store.root.parent, cwd=project).restore(reloaded)
    assert (project / "src" / "app.py").read_text() == "original\n"


def test_prune_keeps_the_most_recent(store: CheckpointStore):
    for turn in range(1, 6):
        store.begin(turn)
    assert store.prune(keep=2) == 3
    assert [c.turn for c in store.existing()] == [5, 4]


def test_a_corrupt_manifest_is_skipped_not_fatal(store: CheckpointStore):
    store.begin(1)
    (store.root / "turn-1" / "manifest.json").write_text("{not json")
    assert store.existing() == []


def test_restore_removes_directories_it_emptied(store: CheckpointStore, project: Path):
    """Creating a file in a new directory must not leave the directory behind after undo."""
    checkpoint = store.begin(1)
    target = project / "newdir" / "deeper" / "file.txt"
    store.capture(checkpoint, target)
    target.parent.mkdir(parents=True)
    target.write_text("created\n")

    store.restore(checkpoint)

    assert not target.exists()
    assert not (project / "newdir").exists(), (
        "an emptied directory is a visible trace of the change"
    )


def test_restore_keeps_a_directory_that_still_has_content(store: CheckpointStore, project: Path):
    checkpoint = store.begin(1)
    target = project / "src" / "extra.py"
    store.capture(checkpoint, target)
    target.write_text("x\n")

    store.restore(checkpoint)

    assert not target.exists()
    assert (project / "src" / "app.py").exists(), "a non-empty parent must survive"
    assert (project / "src").is_dir()


def test_restore_never_removes_the_project_root(store: CheckpointStore, project: Path):
    checkpoint = store.begin(1)
    store.capture(checkpoint, project / "only.py")
    (project / "only.py").write_text("x\n")

    store.restore(checkpoint)

    assert project.is_dir()
