"""Checkpoints: pre-image snapshots, so a change is reversible.

`research/01` §persistence: "Git is the second store: every accepted edit is a commit tagged with
its prompt, so ``/undo`` is real." This module is the *first* store, and it is a snapshot rather than
a commit for three reasons:

- **Precise.** It captures only the files a turn actually modified, so undoing a turn cannot touch
  anything else.
- **Works with untracked files.** ``git stash`` does not capture them by default, and a new file is
  exactly the kind of change a coding agent makes.
- **Hands-off.** Auto-committing to someone's working tree is intrusive and rewrites their index;
  that belongs to the Tier-2 self-update flow, not to a normal edit.

A checkpoint is taken **once per turn, before that turn's first write**, and each file is captured
just before it is first modified in that turn. Capturing every file up front would copy the tree for
nothing; capturing per *edit* would miss the ability to undo a whole turn.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import config


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _prune_empty_parents(directory: Path, stop: Path) -> None:
    """Remove directories an undo has just emptied, up to (but not including) the project root.

    Creating a file in a new directory leaves the directory behind, so an undo that restored the
    file state would still leave a visible trace of the change. Only empty directories are removed,
    and never the project root itself.
    """
    current = directory
    stop = stop.resolve()
    while True:
        try:
            current = current.resolve()
        except OSError:
            return
        if current == stop or stop not in current.parents:
            return
        try:
            if any(current.iterdir()):
                return
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass
class Captured:
    """One file's pre-image."""

    relative: str
    backup: str = ""
    sha256: str = ""
    existed: bool = True


@dataclass
class Checkpoint:
    """A turn's worth of pre-images."""

    turn: int
    directory: Path
    files: dict[str, Captured] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)

    @property
    def manifest_path(self) -> Path:
        return self.directory / "manifest.json"

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(
                {
                    "turn": self.turn,
                    "created_at": self.created_at,
                    "files": {
                        rel: {
                            "backup": cap.backup,
                            "sha256": cap.sha256,
                            "existed": cap.existed,
                        }
                        for rel, cap in self.files.items()
                    },
                },
                indent=2,
            )
        )

    @classmethod
    def load(cls, directory: Path) -> Checkpoint | None:
        manifest = directory / "manifest.json"
        if not manifest.is_file():
            return None
        try:
            raw = json.loads(manifest.read_text())
        except (OSError, ValueError):
            return None
        checkpoint = cls(turn=int(raw.get("turn", 0)), directory=directory)
        checkpoint.created_at = raw.get("created_at", "")
        for rel, entry in (raw.get("files") or {}).items():
            checkpoint.files[rel] = Captured(
                relative=rel,
                backup=entry.get("backup", ""),
                sha256=entry.get("sha256", ""),
                existed=bool(entry.get("existed", True)),
            )
        return checkpoint


class CheckpointStore:
    """Creates and restores checkpoints for one session."""

    def __init__(
        self,
        session_id: str,
        base_dir: Path | None = None,
        cwd: Path | None = None,
    ) -> None:
        """``base_dir`` is the *parent* of the per-session directories, not the session's own.

        It is named for that rather than ``root`` because passing an already-suffixed path silently
        looks in the wrong place — which is exactly how the first version of the reload test failed.
        """
        self.session_id = session_id
        self.root = (base_dir or config.CHECKPOINT_DIR) / session_id
        self.cwd = (cwd or Path.cwd()).resolve()

    # ------------------------------------------------------------------ create

    def begin(self, turn: int) -> Checkpoint:
        checkpoint = Checkpoint(turn=turn, directory=self.root / f"turn-{turn}")
        checkpoint.save()
        return checkpoint

    def capture(self, checkpoint: Checkpoint, path: Path) -> Captured | None:
        """Snapshot ``path`` if this checkpoint has not already captured it."""
        try:
            relative = str(path.resolve().relative_to(self.cwd))
        except ValueError:
            return None
        if relative in checkpoint.files:
            return checkpoint.files[relative]

        files_dir = checkpoint.directory / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        index = len(checkpoint.files) + 1
        backup_name = f"{index:04d}-{path.name}"

        if path.exists():
            data = path.read_bytes()
            (files_dir / backup_name).write_bytes(data)
            captured = Captured(relative=relative, backup=backup_name, sha256=_digest(data))
        else:
            # Absent before the turn: restoring means deleting it again.
            captured = Captured(relative=relative, existed=False)

        checkpoint.files[relative] = captured
        checkpoint.save()
        return captured

    # ------------------------------------------------------------------ restore

    def restore(self, checkpoint: Checkpoint) -> list[str]:
        """Put the captured pre-images back. Returns the paths that changed."""
        changed: list[str] = []
        for relative, captured in sorted(checkpoint.files.items()):
            target = (self.cwd / relative).resolve()
            try:
                target.relative_to(self.cwd)
            except ValueError:
                continue  # refuse to write outside the project, even on undo
            if not captured.existed:
                if target.exists():
                    target.unlink()
                    changed.append(relative)
                    _prune_empty_parents(target.parent, self.cwd)
                continue
            backup = checkpoint.directory / "files" / captured.backup
            if backup.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(backup, target)
                changed.append(relative)
        return changed

    def existing(self) -> list[Checkpoint]:
        """Checkpoints for this session, most recent turn first."""
        if not self.root.is_dir():
            return []
        found: list[Checkpoint] = []
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            checkpoint = Checkpoint.load(directory)
            if checkpoint is not None:
                found.append(checkpoint)
        return sorted(found, key=lambda c: c.turn, reverse=True)

    def latest(self) -> Checkpoint | None:
        found = self.existing()
        return found[0] if found else None

    def prune(self, keep: int = 20) -> int:
        """Delete all but the most recent ``keep`` checkpoints. Returns how many were removed."""
        found = self.existing()
        removed = 0
        for checkpoint in found[keep:]:
            shutil.rmtree(checkpoint.directory, ignore_errors=True)
            removed += 1
        return removed
