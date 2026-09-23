"""Session log (L6): the append-only entry tree.

This is M0 in the architecture's build order, and it comes before the loop because the
transcript tree *is* the product: the TUI, the provider request, resume, fork, and the
future refiner's view of "recent trajectory" are all projections of this one log
(docs/architecture/aiden-architecture.md §7).

v0.1 writes a linear chain (``parentId`` = previous entry). The schema already carries
``id``/``parentId`` so branching and rewind are additive later — no migration.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from . import config

# Entry types. ``session`` is always first; the rest are the transcript.
ENTRY_SESSION = "session"
ENTRY_USER = "user_message"
ENTRY_ASSISTANT = "assistant_message"
ENTRY_TOOL_RESULT = "tool_result"
ENTRY_USAGE = "usage"
ENTRY_DIAGNOSTIC = "diagnostic"
#: An approval decision, recorded because the transcript is the audit trail.
ENTRY_APPROVAL = "approval"
ENTRY_RUN_END = "run_end"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class Entry:
    """One line of the session log.

    ``version`` is present from the first release so a future format change is a migration
    rather than a guess about what an old file means ([arch] §7).
    """

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent_id: str | None = None
    timestamp: str = field(default_factory=_now)
    version: int = config.ENTRY_VERSION

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "parentId": self.parent_id,
                "type": self.type,
                "timestamp": self.timestamp,
                "version": self.version,
                "payload": self.payload,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Entry:
        return cls(
            type=raw.get("type", ""),
            payload=raw.get("payload") or {},
            id=raw.get("id", ""),
            parent_id=raw.get("parentId"),
            timestamp=raw.get("timestamp", ""),
            version=int(raw.get("version", 1)),
        )

    @classmethod
    def from_json(cls, line: str) -> Entry:
        return cls.from_dict(json.loads(line))


class SessionStore:
    """Append-only writer/reader for one session file."""

    def __init__(self, path: Path, session_id: str, entries: list[Entry] | None = None):
        self.path = path
        self.session_id = session_id
        self._entries: list[Entry] = list(entries or [])
        self._fh: IO[str] | None = None

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def create(
        cls,
        *,
        cwd: Path | None = None,
        model: str = "",
        session_id: str | None = None,
    ) -> SessionStore:
        """Create a new session file and write its header.

        Sessions live under ``~/.aiden/sessions/--<project>--/`` — never inside the working
        tree, so a transcript can never be committed ([arch] §12, AGENTS.md rule 6).
        """
        cwd = (cwd or Path.cwd()).resolve()
        session_id = session_id or config.new_session_id()
        directory = config.session_dir(cwd)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{session_id}.jsonl"

        store = cls(path, session_id)
        store.append(
            ENTRY_SESSION,
            {
                "id": session_id,
                "cwd": str(cwd),
                "model": model,
                "harness_version": config.HARNESS_VERSION,
                "started_at": _now(),
            },
        )
        return store

    @classmethod
    def open(cls, path: Path) -> SessionStore:
        """Reopen an existing session (used by tests and future resume)."""
        entries = list(read_entries(path))
        session_id = entries[0].payload.get("id", path.stem) if entries else path.stem
        return cls(path, session_id, entries)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ writing

    def append(self, entry_type: str, payload: dict[str, Any]) -> Entry:
        entry = Entry(
            type=entry_type,
            payload=payload,
            parent_id=self._entries[-1].id if self._entries else None,
        )
        self._entries.append(entry)
        if self._fh is None:
            self._fh = self.path.open("a", encoding="utf-8")
        self._fh.write(entry.to_json() + "\n")
        # fsync per entry: a crashed run must not lose the transcript that explains it
        # ([arch] §7: the entry tree is the product).
        self._fh.flush()
        return entry

    # ------------------------------------------------------------------ reading

    @property
    def entries(self) -> list[Entry]:
        return list(self._entries)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for entry in self._entries:
            counts[entry.type] = counts.get(entry.type, 0) + 1
        header = self._entries[0].payload if self._entries else {}
        return {
            "session_id": self.session_id,
            "path": str(self.path),
            "model": header.get("model", ""),
            "started_at": header.get("started_at", ""),
            "entries": len(self._entries),
            "by_type": counts,
        }


# --------------------------------------------------------------------------- module API


def read_entries(path: Path) -> Iterator[Entry]:
    """Yield entries from a session file, skipping lines that fail to parse.

    A partially-written final line is expected after a crash; tolerating it keeps a damaged
    session readable rather than unusable.
    """
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield Entry.from_json(line)
            except (json.JSONDecodeError, ValueError):
                continue


def list_sessions(cwd: Path | None = None) -> list[dict[str, Any]]:
    """Summaries of sessions for a project, newest first."""
    directory = config.session_dir(cwd)
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl"), reverse=True):
        entries = list(read_entries(path))
        header = entries[0].payload if entries else {}
        out.append(
            {
                "session_id": path.stem,
                "path": str(path),
                "model": header.get("model", ""),
                "started_at": header.get("started_at", ""),
                "entries": len(entries),
                "size_bytes": path.stat().st_size,
            }
        )
    return out


def entry_to_dict(entry: Entry) -> dict[str, Any]:
    """For ``--json`` output and the future TUI reducer."""
    return asdict(entry)
