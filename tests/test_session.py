"""Session log: append-only JSONL, chained ids, crash tolerance."""

from __future__ import annotations

import json
from pathlib import Path

from aiden import config
from aiden.session import (
    ENTRY_ASSISTANT,
    ENTRY_SESSION,
    ENTRY_TOOL_RESULT,
    ENTRY_USER,
    Entry,
    SessionStore,
    list_sessions,
    read_entries,
)


def test_create_writes_header_then_chains_ids(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    store = SessionStore.create(cwd=Path("/tmp/proj"), model="opencode-go/qwen3.8-flash")

    first = store.append(ENTRY_USER, {"text": "hi"})
    second = store.append(ENTRY_ASSISTANT, {"text": "hello"})
    store.close()

    entries = list(read_entries(store.path))
    assert entries[0].type == ENTRY_SESSION
    assert entries[0].parent_id is None
    assert entries[1].id == first.id
    # Linear chain in v0.1: parent id points at the previous entry, so branching is additive.
    assert entries[1].parent_id == entries[0].id
    assert entries[2].parent_id == first.id
    assert second.parent_id == first.id


def test_every_line_is_valid_json_with_required_fields(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    store = SessionStore.create(cwd=Path("/tmp/proj"), model="m")
    store.append(ENTRY_USER, {"text": "x"})
    store.close()

    for line in store.path.read_text().splitlines():
        raw = json.loads(line)
        assert set(raw) >= {"id", "parentId", "type", "timestamp", "version", "payload"}
        assert raw["version"] == config.ENTRY_VERSION


def test_header_records_provenance(tmp_path: Path, monkeypatch):
    """A run must be attributable to a model, a cwd and a harness version."""
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    store = SessionStore.create(cwd=Path("/tmp/proj"), model="anthropic/claude-haiku-4-5")
    store.close()

    header = next(iter(read_entries(store.path))).payload
    assert header["model"] == "anthropic/claude-haiku-4-5"
    # create() resolves the cwd, which on macOS turns /tmp into /private/tmp
    assert header["cwd"] == str(Path("/tmp/proj").resolve())
    assert header["harness_version"] == config.HARNESS_VERSION


def test_sessions_never_written_inside_the_repo(tmp_path: Path, monkeypatch):
    """A transcript must not be committable (AGENTS.md rule 6)."""
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    store = SessionStore.create(cwd=Path.cwd(), model="m")
    store.close()
    assert Path.cwd() not in store.path.parents


def test_partial_final_line_is_tolerated(tmp_path: Path, monkeypatch):
    """A crashed run leaves a truncated last line; the rest must stay readable."""
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    store = SessionStore.create(cwd=Path("/tmp/proj"), model="m")
    store.append(ENTRY_USER, {"text": "complete"})
    store.close()

    with store.path.open("a") as fh:
        fh.write('{"id":"broken","type":"tool_resu')
    with store.path.open("a") as fh:
        fh.write('\n{"id":"ok2","type":"diagnostic","payload":{},"parentId":null}\n')

    entries = list(read_entries(store.path))
    assert [e.type for e in entries] == [ENTRY_SESSION, ENTRY_USER, "diagnostic"]


def test_reopen_reconstructs_entries(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    store = SessionStore.create(cwd=Path("/tmp/proj"), model="m")
    store.append(ENTRY_TOOL_RESULT, {"name": "read", "output": "x"})
    store.close()

    reopened = SessionStore.open(store.path)
    assert len(reopened.entries) == 2
    assert reopened.session_id == store.session_id
    # Appending after reopen continues the chain rather than starting a new one.
    entry = reopened.append(ENTRY_USER, {"text": "more"})
    assert entry.parent_id == reopened.entries[-2].id
    reopened.close()


def test_tool_result_payload_is_round_trippable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    store = SessionStore.create(cwd=Path("/tmp/proj"), model="m")
    payload = {"call_id": "c1", "name": "grep", "output": "a\nb", "is_error": False}
    store.append(ENTRY_TOOL_RESULT, payload)
    store.close()
    assert list(read_entries(store.path))[1].payload == payload


def test_list_sessions_newest_first(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    cwd = Path("/tmp/proj")
    for sid in ("20260101T000000-aaaaaaaa", "20260102T000000-bbbbbbbb"):
        store = SessionStore.create(cwd=cwd, model="m", session_id=sid)
        store.close()

    rows = list_sessions(cwd)
    assert [r["session_id"] for r in rows] == [
        "20260102T000000-bbbbbbbb",
        "20260101T000000-aaaaaaaa",
    ]


def test_list_sessions_empty_for_unknown_project(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    assert list_sessions(Path("/tmp/never-used")) == []


def test_entry_json_round_trip():
    entry = Entry(type="user_message", payload={"text": "hi"}, id="abc", parent_id=None)
    assert Entry.from_json(entry.to_json()).payload == {"text": "hi"}
