"""Read-only tools: budgets, refusals, and never raising.

Every test here maps to an acceptance criterion in docs/plan/v0.1-minimal-harness.md §2:
oversized output truncates with a continuation hint, ``../`` escapes are refused, and an
empty search says so explicitly instead of returning "".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiden import config
from aiden.tools import TOOLS, ToolContext, execute, tool_specs
from aiden.tools.types import resolve_in_cwd


@pytest.fixture
def project(tmp_path: Path) -> Path:
    # The project is a *subdirectory* of tmp_path so tmp_path can hold genuinely-outside
    # files; otherwise a "path escape" test would be testing nothing.
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "import os\n\n\ndef main():\n    # TODO: implement\n    return os.getcwd()\n"
    )
    (root / "README.md").write_text("# Title\n\nSome prose about the project.\n")
    (root / "big.txt").write_text("\n".join(f"line {i}" for i in range(1, 1201)))
    (root / "binary.bin").write_bytes(b"\x00\x01\x02binary")
    (root / "secret").mkdir()
    (root / "secret" / "inside.txt").write_text("inside the project")
    return root


@pytest.fixture
def ctx(project: Path, tmp_path: Path) -> ToolContext:
    spill = tmp_path / "spill"
    return ToolContext(cwd=project, spill_dir=spill)


# --------------------------------------------------------------------------- registry


def test_registry_exposes_the_three_read_only_tools():
    assert set(TOOLS) == {"read", "grep", "glob"}


def test_specs_are_valid_json_schema_shapes():
    for spec in tool_specs():
        assert spec.name and spec.description
        assert spec.parameters.get("type") == "object"
        assert "properties" in spec.parameters


def test_unknown_tool_is_an_error_result_not_an_exception(ctx: ToolContext):
    result = execute("nope", {}, ctx)
    assert result.is_error
    assert "unknown tool" in result.output
    assert "read" in result.output  # lists what is available so the model can recover


def test_non_object_arguments_are_rejected(ctx: ToolContext):
    result = execute("read", ["not", "a", "dict"], ctx)
    assert result.is_error
    assert "JSON object" in result.output


def test_a_buggy_tool_cannot_kill_the_run(ctx: ToolContext, monkeypatch):
    """A tool bug is reported to the model; the run must survive it."""
    from aiden.tools import read as read_module

    def boom(self, args, ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(read_module.ReadTool, "run", boom)
    result = execute("read", {"path": "README.md"}, ctx)
    assert result.is_error
    assert "kaboom" in result.output


# --------------------------------------------------------------------------- read


def test_read_returns_numbered_lines(ctx: ToolContext):
    result = execute("read", {"path": "README.md"}, ctx)
    assert not result.is_error
    assert "1\t# Title" in result.output
    assert result.meta["lines"] == 3  # trailing newline does not create a line


def test_read_pages_with_offset_and_hint(ctx: ToolContext):
    first = execute("read", {"path": "big.txt", "limit": 10}, ctx)
    assert "line 1" in first.output
    assert "offset=11" in first.hint

    second = execute("read", {"path": "big.txt", "offset": 11, "limit": 10}, ctx)
    assert "line 11" in second.output
    assert "line 1\n" not in second.output


def test_oversized_read_truncates_with_continuation_hint(ctx: ToolContext):
    result = execute("read", {"path": "big.txt"}, ctx)
    assert result.truncated
    assert "offset=401" in result.hint  # READ_MAX_LINES default
    assert "of 1200" in result.hint
    assert result.output.count("\n") < config.READ_MAX_LINES + 1
    assert result.meta["remaining"] == 800


def test_fully_read_file_is_not_flagged_truncated(ctx: ToolContext):
    """Do not cry wolf: a complete read must report truncated=False with no hint."""
    result = execute("read", {"path": "README.md"}, ctx)
    assert not result.truncated
    assert result.hint == ""
    assert result.meta["remaining"] == 0


def test_read_byte_budget_truncates_within_a_single_window(ctx: ToolContext, project: Path):
    """A window whose bytes exceed the budget is cut and says so explicitly."""
    (project / "wide.txt").write_text("\n".join("x" * 400 for _ in range(200)))
    result = execute("read", {"path": "wide.txt"}, ctx)
    assert result.truncated
    assert "truncated" in result.hint
    assert "byte budget" in result.hint
    assert len(result.output.encode()) <= config.READ_MAX_BYTES * 1.1


def test_read_refuses_binary_files(ctx: ToolContext):
    result = execute("read", {"path": "binary.bin"}, ctx)
    assert result.is_error
    assert "binary" in result.output


def test_read_refuses_paths_outside_cwd(ctx: ToolContext, project: Path):
    result = execute("read", {"path": "../etc/passwd"}, ctx)
    assert result.is_error
    assert "outside the working directory" in result.output


def test_read_refuses_absolute_path_outside_cwd(ctx: ToolContext):
    result = execute("read", {"path": "/etc/passwd"}, ctx)
    assert result.is_error
    assert "outside" in result.output


def test_read_refuses_symlink_escaping_the_tree(ctx: ToolContext, project: Path, tmp_path: Path):
    outside = tmp_path / "outside-real.txt"
    outside.write_text("secret")
    link = project / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    result = execute("read", {"path": "link.txt"}, ctx)
    assert result.is_error
    assert "outside" in result.output


def test_read_missing_file_is_a_clear_error(ctx: ToolContext):
    result = execute("read", {"path": "nope.txt"}, ctx)
    assert result.is_error
    assert "no such file" in result.output


def test_read_directory_lists_entries_instead_of_failing(ctx: ToolContext):
    result = execute("read", {"path": "src"}, ctx)
    assert not result.is_error
    assert "app.py" in result.output


def test_read_offset_past_eof_is_reported(ctx: ToolContext):
    result = execute("read", {"path": "README.md", "offset": 9999}, ctx)
    assert result.is_error
    assert "past the end" in result.output


def test_read_records_content_hash_for_future_edit_guard(ctx: ToolContext, project: Path):
    execute("read", {"path": "README.md"}, ctx)
    assert str(project / "README.md") in ctx.read_state.hashes


# --------------------------------------------------------------------------- grep


def test_grep_default_files_first_is_cheap(ctx: ToolContext):
    result = execute("grep", {"pattern": "TODO"}, ctx)
    assert not result.is_error
    assert "src/app.py" in result.output
    assert "path:count" in result.output
    # files-first must not leak the matching line's content
    assert "implement" not in result.output


def test_grep_content_mode_returns_line_numbers(ctx: ToolContext):
    result = execute("grep", {"pattern": "TODO", "mode": "content"}, ctx)
    assert "src/app.py:5:" in result.output
    assert "implement" in result.output


def test_grep_empty_result_is_explicit_not_blank(ctx: ToolContext):
    """A bare empty string reads as failure and derails the model (research/02 §3)."""
    result = execute("grep", {"pattern": "zzz_no_such_symbol_zzz"}, ctx)
    assert not result.is_error
    assert "no matches" in result.output
    assert result.output.strip() != ""


def test_grep_context_is_opt_in(ctx: ToolContext):
    without = execute("grep", {"pattern": "TODO", "mode": "content"}, ctx)
    with_context = execute("grep", {"pattern": "TODO", "mode": "content", "context": 2}, ctx)
    assert len(with_context.output) > len(without.output)


def test_grep_invalid_regex_is_an_error_result(ctx: ToolContext):
    result = execute("grep", {"pattern": "([unclosed"}, ctx)
    assert result.is_error
    assert "regular expression" in result.output or "regex" in result.output.lower()


def test_grep_rejects_unknown_mode(ctx: ToolContext):
    result = execute("grep", {"pattern": "x", "mode": "everything"}, ctx)
    assert result.is_error
    assert "unknown mode" in result.output


def test_grep_requires_a_pattern(ctx: ToolContext):
    result = execute("grep", {}, ctx)
    assert result.is_error
    assert "pattern is required" in result.output


def test_grep_refuses_paths_outside_cwd(ctx: ToolContext):
    result = execute("grep", {"pattern": "x", "path": "../"}, ctx)
    assert result.is_error
    assert "outside" in result.output


def test_grep_truncates_at_the_hit_cap(ctx: ToolContext):
    result = execute("grep", {"pattern": "line", "mode": "content", "max_results": 5}, ctx)
    assert result.truncated
    assert "truncated" in result.hint
    assert result.meta["returned"] == 5


def test_grep_skips_noise_directories(ctx: ToolContext, project: Path):
    noisy = project / "node_modules"
    noisy.mkdir()
    (noisy / "dep.js").write_text("TODO inside a dependency")
    result = execute("grep", {"pattern": "TODO", "mode": "content"}, ctx)
    assert "node_modules" not in result.output


# --------------------------------------------------------------------------- glob


def test_glob_finds_files_by_pattern(ctx: ToolContext):
    result = execute("glob", {"pattern": "**/*.py"}, ctx)
    assert "src/app.py" in result.output


def test_glob_empty_result_is_explicit(ctx: ToolContext):
    result = execute("glob", {"pattern": "**/*.rs"}, ctx)
    assert not result.is_error
    assert "no files match" in result.output


def test_glob_rejects_parent_traversal(ctx: ToolContext):
    result = execute("glob", {"pattern": "../**/*.py"}, ctx)
    assert result.is_error
    assert "relative to the working directory" in result.output


def test_glob_rejects_absolute_pattern(ctx: ToolContext):
    result = execute("glob", {"pattern": "/etc/**"}, ctx)
    assert result.is_error


def test_glob_truncates_at_the_path_cap(ctx: ToolContext, project: Path):
    for i in range(20):
        (project / f"many_{i}.txt").write_text("x")
    result = execute("glob", {"pattern": "many_*.txt", "max_paths": 5}, ctx)
    assert result.truncated
    assert result.meta["returned"] == 5
    assert result.meta["matches"] == 20


# --------------------------------------------------------------------------- path guard


def test_resolve_in_cwd_accepts_nested_relative_paths(project: Path):
    path, refusal = resolve_in_cwd("src/app.py", project)
    assert path is not None and refusal == ""


def test_resolve_in_cwd_rejects_empty_path(project: Path):
    path, refusal = resolve_in_cwd("", project)
    assert path is None
    assert "empty" in refusal


def test_grep_enforces_a_byte_budget_and_spills(ctx: ToolContext, project: Path):
    """The hit cap alone is not a budget: long lines can still blow the window."""
    (project / "wide.log").write_text("\n".join("x" * 500 + "TODO" for _ in range(200)))
    result = execute("grep", {"pattern": "TODO", "mode": "content"}, ctx)

    assert result.truncated
    assert result.meta.get("bytes_capped") is True
    assert len(result.output.encode()) <= config.GREP_MAX_BYTES * 1.2
    # The full output was spilled so nothing is silently lost.
    assert "written to" in result.hint
    spill_path = Path(result.hint.split("written to ")[1].rstrip(".]"))
    assert spill_path.is_file()
    assert "x" * 500 in spill_path.read_text()


def test_grep_does_not_spill_small_results(ctx: ToolContext):
    result = execute("grep", {"pattern": "TODO", "mode": "content"}, ctx)
    assert result.meta.get("bytes_capped") is None
    assert "written to" not in result.hint


def test_glob_skips_local_scratch_directories(ctx: ToolContext, project: Path):
    (project / ".aiden-research").mkdir()
    (project / ".aiden-research" / "launch.py").write_text("x")
    (project / ".pi").mkdir()
    (project / ".pi" / "mcp.json").write_text("{}")
    # Hidden project content like .github must stay visible.
    (project / ".github").mkdir()
    (project / ".github" / "ci.yml").write_text("on: push")

    result = execute("glob", {"pattern": "**/*"}, ctx)
    assert ".aiden-research" not in result.output
    assert ".pi/" not in result.output
    assert ".github/ci.yml" in result.output


def test_grep_paths_are_relative_to_the_working_directory(ctx: ToolContext, project: Path):
    """Absolute paths are long, wrap badly, and are not what `read` expects."""
    result = execute("grep", {"pattern": "TODO", "mode": "content"}, ctx)
    assert str(project) not in result.output, "an absolute path leaked into grep output"
    assert "src/app.py:" in result.output


def test_grep_paths_stay_relative_when_the_search_root_is_a_file(ctx: ToolContext, project: Path):
    """Regression: stripping the search root failed here, because ripgrep emits '<file>:<line>:'."""
    result = execute("grep", {"pattern": "TODO", "mode": "content", "path": "src/app.py"}, ctx)
    assert not result.is_error
    assert str(project) not in result.output
    assert result.output.lstrip().startswith("1 ") or "src/app.py:" in result.output
