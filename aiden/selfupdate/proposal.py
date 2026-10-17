"""A self-update proposal: what would change, why, and what it claims to improve.

The representation is full file contents rather than a patch, deliberately. A patch has to be applied
to be understood, and "apply this and see" is the wrong shape for something a human must review
before it runs — the review step needs the resulting file, and the diff for review is *rendered*
from old and new content with the same `diffutil` the write tools use, so the reviewer sees exactly
what the approval UI would show.

`None` as content means delete. A proposal that could only add and modify files could not remove the
behaviour it was replacing.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..diffutil import unified


@dataclass(slots=True)
class Proposal:
    """One proposed change to Aiden, ready to be evaluated and gated."""

    title: str
    rationale: str
    claimed_improvement: str
    #: Repo-relative path -> full new contents, or ``None`` to delete the file.
    files: dict[str, str | None] = field(default_factory=dict)
    #: Where the claim comes from — a session id, an issue, a failing test. Reviewable evidence, not
    #: decoration: "why do you believe this" is the first question a reviewer asks.
    evidence: list[str] = field(default_factory=list)
    base_commit: str = "HEAD"
    id: str = field(default_factory=lambda: f"proposal-{uuid.uuid4().hex[:8]}")

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("a proposal needs a title")
        if not self.rationale.strip():
            raise ValueError(
                "a proposal needs a rationale; an unexplained change cannot be reviewed"
            )
        if not self.files:
            raise ValueError("a proposal with no file changes proposes nothing")

    # ------------------------------------------------------------------ views

    def changed_paths(self) -> list[str]:
        return sorted(self.files)

    def deletes(self) -> list[str]:
        return sorted(path for path, content in self.files.items() if content is None)

    def render_diff(self, repo: Path) -> str:
        """The unified diff a human reviews, rendered from the current contents to the proposed."""
        chunks: list[str] = []
        for path in self.changed_paths():
            target = repo / path
            old = target.read_text(encoding="utf-8") if target.exists() else ""
            new = self.files[path]
            if new is None:
                # A deletion has no "new" side; showing an empty file with every line removed states
                # it without inventing a marker the reviewer has to interpret.
                chunks.append(unified(old, "", path))
            else:
                chunks.append(unified(old, new, path))
        return "".join(chunks)

    def commit_message(self, *, eval_trailer: str, outcome: str = "") -> str:
        """The commit a promotion writes.

        The eval report id travels in an `Aiden-Eval` trailer, which is how rollback is a `git log`
        lookup rather than an investigation (research/03 §Versioning and rollback).
        """
        body = [f"selfupdate: {self.title}", "", self.rationale.strip(), ""]
        body.append(f"Proposal: {self.id}")
        body.append(f"Claimed: {self.claimed_improvement}")
        if outcome:
            body.append(f"Gate: {outcome}")
        if self.evidence:
            body.append("Evidence:")
            body.extend(f"- {item}" for item in self.evidence)
        body.extend(["", f"Aiden-Eval: {eval_trailer}"])
        return "\n".join(body) + "\n"

    # ------------------------------------------------------------------ persistence

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "rationale": self.rationale,
            "claimed_improvement": self.claimed_improvement,
            "files": self.files,
            "evidence": list(self.evidence),
            "base_commit": self.base_commit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Proposal:
        raw_files = data.get("files") or {}
        if not isinstance(raw_files, dict):
            raise ValueError("`files` must be an object of path -> contents, with null to delete")
        files: dict[str, str | None] = {}
        for path, content in raw_files.items():
            if content is not None and not isinstance(content, str):
                raise ValueError(f"{path}: contents must be a string or null")
            files[str(path)] = content
        raw_evidence = data.get("evidence") or []
        if not isinstance(raw_evidence, list):
            raise ValueError("`evidence` must be a list of strings")
        return cls(
            title=str(data.get("title", "")),
            rationale=str(data.get("rationale", "")),
            claimed_improvement=str(data.get("claimed_improvement", "")),
            files=files,
            evidence=[str(item) for item in raw_evidence],
            base_commit=str(data.get("base_commit", "HEAD")),
            id=str(data.get("id") or f"proposal-{uuid.uuid4().hex[:8]}"),
        )

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path


def load(path: Path) -> Proposal:
    """Read a proposal from JSON.

    Errors carry the path and the field, because "which file was wrong" is the only thing a person
    running this needs to know.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"no proposal at {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    try:
        return Proposal.from_dict(data)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc
