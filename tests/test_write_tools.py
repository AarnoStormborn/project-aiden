"""Write tools: every guard, in the order the chain specifies.

These are the security-relevant paths. Each test names the failure it prevents, because a guard
without a test is a guard that will be silently removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiden.tools import MUTATING_TOOLS, TOOLS, ToolContext, execute
from aiden.tools.policy import WritePolicy
from aiden.tools.types import ReadState


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def main():\n    return 1\n")
    (root / "README.md").write_text("# Title\n\nSome prose.\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    return root


@pytest.fixture
def ctx(project: Path, tmp_path: Path) -> ToolContext:
    return ToolContext(
        cwd=project,
        read_state=ReadState(),
        spill_dir=tmp_path / "spill",
        write_policy=WritePolicy(),
    )


def read(path: str, ctx: ToolContext) -> None:
    """Satisfy read-before-edit, as the model must."""
    result = execute("read", {"path": path}, ctx)
    assert not result.is_error, result.output


# --------------------------------------------------------------------------- mutating flag


def test_write_tools_are_registered_as_mutating():
    assert MUTATING_TOOLS == {"edit", "write"}
    assert TOOLS["edit"].mutating is True
    assert TOOLS["write"].mutating is True


# --------------------------------------------------------------------------- edit: happy path


def test_edit_replaces_a_unique_string(ctx: ToolContext, project: Path):
    read("src/app.py", ctx)
    result = execute(
        "edit",
        {"path": "src/app.py", "old_string": "return 1", "new_string": "return 2"},
        ctx,
    )
    assert not result.is_error, result.output
    assert "return 2" in (project / "src" / "app.py").read_text()


def test_edit_updates_the_read_hash_so_a_second_edit_works(ctx: ToolContext):
    read("src/app.py", ctx)
    execute("edit", {"path": "src/app.py", "old_string": "return 1", "new_string": "return 2"}, ctx)
    second = execute(
        "edit", {"path": "src/app.py", "old_string": "return 2", "new_string": "return 3"}, ctx
    )
    assert not second.is_error, "an edit must refresh the hash it just changed"


# --------------------------------------------------------------------------- edit: guards


def test_edit_without_reading_first_is_refused(ctx: ToolContext):
    """Read-before-edit is the guard that stops edits based on a guess about the file."""
    result = execute(
        "edit", {"path": "src/app.py", "old_string": "return 1", "new_string": "return 2"}, ctx
    )
    assert result.is_error
    assert "has not been read" in result.output


def test_edit_is_refused_when_the_file_changed_since_reading(ctx: ToolContext, project: Path):
    """Hashing, not mtime: an external change must invalidate the model's view."""
    read("src/app.py", ctx)
    (project / "src" / "app.py").write_text("def main():\n    return 99\n")
    result = execute(
        "edit", {"path": "src/app.py", "old_string": "return 99", "new_string": "return 2"}, ctx
    )
    assert result.is_error
    assert "changed since you read it" in result.output


def test_edit_refuses_an_ambiguous_match_and_lists_the_lines(ctx: ToolContext, project: Path):
    (project / "dup.py").write_text("x = 1\ny = 2\nx = 1\n")
    read("dup.py", ctx)
    result = execute("edit", {"path": "dup.py", "old_string": "x = 1", "new_string": "x = 9"}, ctx)
    assert result.is_error
    assert "appears 2 times" in result.output
    assert "lines 1, 3" in result.output
    # Nothing was written.
    assert (project / "dup.py").read_text().count("x = 1") == 2


def test_replace_all_changes_every_occurrence(ctx: ToolContext, project: Path):
    (project / "dup.py").write_text("x = 1\ny = 2\nx = 1\n")
    read("dup.py", ctx)
    result = execute(
        "edit",
        {"path": "dup.py", "old_string": "x = 1", "new_string": "x = 9", "replace_all": True},
        ctx,
    )
    assert not result.is_error, result.output
    assert (project / "dup.py").read_text().count("x = 9") == 2


def test_edit_reports_a_missing_match_with_the_closest_text(ctx: ToolContext):
    read("src/app.py", ctx)
    result = execute(
        "edit",
        {"path": "src/app.py", "old_string": "def main():\n    return 42", "new_string": "x"},
        ctx,
    )
    assert result.is_error
    assert "was not found" in result.output
    assert "Closest text" in result.output


def test_edit_recovers_from_a_whitespace_mismatch_and_says_so(ctx: ToolContext, project: Path):
    """Indentation mismatches are the most common benign failure; the retry is reported."""
    read("src/app.py", ctx)
    result = execute(
        "edit",
        {
            "path": "src/app.py",
            "old_string": "def main():\nreturn 1",
            "new_string": "def main():\n    return 7",
        },
        ctx,
    )
    assert not result.is_error, result.output
    assert "whitespace differences ignored" in result.output
    assert "return 7" in (project / "src" / "app.py").read_text()


def test_edit_needs_a_real_file(ctx: ToolContext):
    result = execute("edit", {"path": "nope.py", "old_string": "a", "new_string": "b"}, ctx)
    assert result.is_error
    assert "no such file" in result.output
    assert "use write" in result.output


def test_edit_refuses_an_empty_old_string(ctx: ToolContext):
    read("src/app.py", ctx)
    result = execute("edit", {"path": "src/app.py", "old_string": "", "new_string": "x"}, ctx)
    assert result.is_error
    assert "old_string is empty" in result.output


def test_edit_refuses_a_no_op(ctx: ToolContext):
    read("src/app.py", ctx)
    result = execute(
        "edit", {"path": "src/app.py", "old_string": "return 1", "new_string": "return 1"}, ctx
    )
    assert result.is_error
    assert "identical" in result.output


def test_edit_refuses_a_binary_file(ctx: ToolContext, project: Path):
    """Binary is checked before read-state, so the refusal names the real problem."""
    (project / "blob.bin").write_bytes(b"\x00\x01\x02")
    result = execute("edit", {"path": "blob.bin", "old_string": "a", "new_string": "b"}, ctx)
    assert result.is_error
    assert "binary" in result.output


def test_edit_refuses_a_path_outside_the_project(ctx: ToolContext):
    result = execute("edit", {"path": "../outside.py", "old_string": "a", "new_string": "b"}, ctx)
    assert result.is_error
    assert "outside" in result.output


# --------------------------------------------------------------------------- edit: validation


def test_a_broken_edit_is_applied_but_reported(ctx: ToolContext, project: Path):
    """Keeping it and saying so is more useful than a silent revert: the error is the information."""
    read("src/app.py", ctx)
    result = execute(
        "edit", {"path": "src/app.py", "old_string": "return 1", "new_string": "return ("}, ctx
    )
    assert result.is_error, "a file that no longer parses must be reported as an error"
    assert "no longer parses" in result.output
    assert result.meta.get("applied") is True
    assert "return (" in (project / "src" / "app.py").read_text()


def test_editing_non_python_skips_the_parse_gate(ctx: ToolContext, project: Path):
    read("README.md", ctx)
    result = execute(
        "edit",
        {"path": "README.md", "old_string": "Some prose.", "new_string": "Other prose."},
        ctx,
    )
    assert not result.is_error


# --------------------------------------------------------------------------- write


def test_write_creates_a_new_file(ctx: ToolContext, project: Path):
    result = execute("write", {"path": "src/new.py", "content": "x = 1\n"}, ctx)
    assert not result.is_error, result.output
    assert (project / "src" / "new.py").read_text() == "x = 1\n"
    assert result.meta["created"] is True


def test_write_creates_missing_parent_directories(ctx: ToolContext, project: Path):
    result = execute("write", {"path": "a/b/c.py", "content": "x = 1\n"}, ctx)
    assert not result.is_error, result.output
    assert (project / "a" / "b" / "c.py").is_file()


def test_write_refuses_to_overwrite_an_unread_file(ctx: ToolContext):
    result = execute("write", {"path": "src/app.py", "content": "clobbered\n"}, ctx)
    assert result.is_error
    assert "has not been read" in result.output


def test_write_allows_overwriting_a_read_file(ctx: ToolContext, project: Path):
    read("src/app.py", ctx)
    result = execute("write", {"path": "src/app.py", "content": "def main():\n    return 5\n"}, ctx)
    assert not result.is_error, result.output
    assert "return 5" in (project / "src" / "app.py").read_text()


def test_write_refuses_a_large_overwrite_and_points_at_edit(ctx: ToolContext, project: Path):
    big = project / "big.py"
    big.write_text("\n".join(f"x{i} = {i}" for i in range(300)) + "\n")
    read("big.py", ctx)
    result = execute("write", {"path": "big.py", "content": "x = 1\n"}, ctx)
    assert result.is_error
    assert "Use edit" in result.output
    assert "x0 = 0" in big.read_text(), "the file must be untouched"


def test_write_reports_broken_python(ctx: ToolContext):
    result = execute("write", {"path": "src/bad.py", "content": "def (:\n"}, ctx)
    assert result.is_error
    assert "does not parse" in result.output


def test_write_reports_broken_json(ctx: ToolContext):
    result = execute("write", {"path": "data.json", "content": "{not json"}, ctx)
    assert result.is_error
    assert "invalid JSON" in result.output


def test_write_refuses_a_directory(ctx: ToolContext):
    result = execute("write", {"path": "src", "content": "x"}, ctx)
    assert result.is_error
    assert "directory" in result.output


# --------------------------------------------------------------------------- policy


def test_writing_into_dot_git_is_refused(ctx: ToolContext):
    """Writing to .git corrupts the rollback path the harness depends on."""
    result = execute("write", {"path": ".git/config", "content": "clobbered"}, ctx)
    assert result.is_error
    assert "refused" in result.output
    assert "rollback" in result.output


def test_policy_flags_sensitive_paths_without_blocking_them(tmp_path: Path, project: Path):
    """Secrets are allowed but flagged, because a rubber-stamped yes is most expensive there."""
    ctx = ToolContext(cwd=project, write_policy=WritePolicy())
    _path, decision = ctx.policy().check(".env", project)
    assert decision.allowed
    assert decision.sensitive is True


def test_policy_can_allow_a_path_explicitly(ctx: ToolContext, project: Path):
    ctx.write_policy = WritePolicy(allow_extra=(".git",))
    result = execute("write", {"path": ".git/hooks/pre-commit", "content": "#!/bin/sh\n"}, ctx)
    assert not result.is_error, result.output


def test_an_allowlisted_path_still_needs_read_before_overwrite(ctx: ToolContext):
    """Path policy and read-before-overwrite are independent guards; allowlisting one is not the other."""
    ctx.write_policy = WritePolicy(allow_extra=(".git",))
    result = execute("write", {"path": ".git/config", "content": "clobbered"}, ctx)
    assert result.is_error
    assert "has not been read" in result.output


def test_policy_defaults_include_git_and_secrets():
    policy = WritePolicy()
    assert ".git/**" in policy.never
    assert ".env" in policy.sensitive
    assert "never:" in policy.describe()


def test_policy_reads_a_user_allowlist_from_the_environment(monkeypatch):
    monkeypatch.setenv("AIDEN_WRITE_ALLOW", "generated/**, vendor/*")
    policy = WritePolicy.from_env()
    assert policy.allow_extra == ("generated/**", "vendor/*")


def test_dot_git_relative_path_is_matched_not_the_absolute_one(tmp_path: Path, project: Path):
    """Regression guard: matching absolute paths would make the .git rule never fire."""
    policy = WritePolicy()
    _path, decision = policy.check(".git/HEAD", project)
    assert not decision.allowed
