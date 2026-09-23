"""The bash tool: bounds on time, output and input, and the gate it goes through.

The timeout test is the important one: a shell that leaves children running is worse than no timeout,
so the kill has to reach the process group.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aiden import config
from aiden.tools import TOOLS, ToolContext, execute
from aiden.tools.bash import DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S, BashTool
from aiden.tools.command_policy import CommandPolicy
from aiden.tools.types import ReadState, preview_of


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch) -> ToolContext:
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(config, "SPILL_DIR", tmp_path / "spill")
    return ToolContext(
        cwd=project,
        read_state=ReadState(),
        spill_dir=tmp_path / "spill",
        command_policy=CommandPolicy(),
    )


def run(command: str, ctx: ToolContext, **kw):
    return execute("bash", {"command": command, **kw}, ctx)


# --------------------------------------------------------------------------- basics


def test_a_command_runs_and_reports_its_exit_code(ctx: ToolContext):
    result = run("echo hello", ctx)
    assert "hello" in result.output
    assert "exit 0" in result.output
    assert result.meta["exit_code"] == 0


def test_a_failing_command_is_an_error_result_not_an_exception(ctx: ToolContext):
    result = run("exit 3", ctx)
    assert result.is_error
    assert "exit 3" in result.output


def test_the_working_directory_is_the_project(ctx: ToolContext):
    """Compare resolved paths: on macOS /tmp is /private/tmp, and the shell reports the real one."""
    result = run("pwd", ctx)
    reported = Path(result.output.splitlines()[-1]).resolve()
    assert reported == ctx.cwd.resolve()


def test_stderr_is_included(ctx: ToolContext):
    result = run("echo oops >&2", ctx)
    assert "oops" in result.output


def test_an_empty_command_is_refused(ctx: ToolContext):
    result = run("   ", ctx)
    assert result.is_error
    assert "required" in result.output


def test_the_command_is_recorded_in_meta(ctx: ToolContext):
    result = run("echo x", ctx)
    assert result.meta["command"] == "echo x"


# --------------------------------------------------------------------------- stdin


def test_stdin_is_closed_so_interactive_commands_fail_fast(ctx: ToolContext):
    """An interactive command would hang the run; research/02 §7 says it needs background+poll."""
    started = time.perf_counter()
    result = run("cat", ctx)  # cat with no arguments reads stdin
    elapsed = time.perf_counter() - started

    assert elapsed < 5, "a command reading stdin must not hang"
    assert result.meta["exit_code"] is not None


# --------------------------------------------------------------------------- timeout


def test_a_command_that_never_exits_is_killed(ctx: ToolContext):
    started = time.perf_counter()
    result = run("sleep 30", ctx, timeout=1)
    elapsed = time.perf_counter() - started

    assert elapsed < 10, "the timeout did not take effect"
    assert result.meta["timed_out"] is True
    assert "killed after" in result.output
    assert result.is_error


def test_the_timeout_kills_the_whole_process_group(ctx: ToolContext):
    """A shell that leaves children running is worse than no timeout at all."""
    marker = ctx.cwd / "child-survived.txt"
    # The child would write the marker after 3s; it must be killed before it can.
    result = run(f"(sleep 3; echo alive > {marker.name}) & sleep 30", ctx, timeout=1)
    assert result.meta["timed_out"] is True

    time.sleep(3.5)
    assert not marker.exists(), "the background child outlived the timeout"


def test_timeout_is_clamped_to_the_ceiling(ctx: ToolContext):
    from aiden.tools.bash import _timeout

    assert _timeout(10_000) == MAX_TIMEOUT_S
    assert _timeout(0) == 1
    assert _timeout(None) == DEFAULT_TIMEOUT_S
    assert _timeout("nonsense") == DEFAULT_TIMEOUT_S


def test_killing_reports_partial_output(ctx: ToolContext):
    result = run("echo before; sleep 30", ctx, timeout=1)
    assert "before" in result.output, "output produced before the kill must be preserved"


# --------------------------------------------------------------------------- output budget


def test_huge_output_is_elided_with_a_head_and_tail(ctx: ToolContext):
    result = run("seq 1 5000", ctx)
    assert result.truncated
    assert "1" in result.output
    assert "5000" in result.output, "the tail is usually the answer, so it must survive"
    assert "lines elided" in result.output
    assert len(result.output.encode()) <= config.READ_MAX_BYTES * 1.5


def test_very_large_output_spills_to_a_file(ctx: ToolContext):
    result = run("seq 1 200000", ctx)
    assert "the full output is at" in result.hint
    spill_path = Path(result.hint.split("the full output is at ")[1].rstrip("]"))
    assert spill_path.is_file()
    assert "200000" in spill_path.read_text()


def test_small_output_is_not_elided(ctx: ToolContext):
    result = run("echo one; echo two", ctx)
    assert not result.truncated
    assert result.hint == ""


# --------------------------------------------------------------------------- gate


def test_read_only_commands_do_not_need_approval(ctx: ToolContext):
    assert TOOLS["bash"].approval_required({"command": "git status"}, ctx) is False
    assert TOOLS["bash"].approval_required({"command": "ls -la"}, ctx) is False


def test_unprovable_commands_need_approval(ctx: ToolContext):
    for command in ("rm -rf build", "ls > out.txt", "ls && rm -rf x", "find . -delete"):
        assert TOOLS["bash"].approval_required({"command": command}, ctx) is True, command


def test_bash_is_registered_as_mutating():
    from aiden.tools import MUTATING_TOOLS

    assert TOOLS["bash"].mutating is True
    assert "bash" in MUTATING_TOOLS


def test_a_read_only_command_has_no_preview(ctx: ToolContext):
    """No preview means no approval prompt: there is nothing for a human to decide."""
    assert preview_of(TOOLS["bash"], {"command": "git status"}, ctx) is None


def test_a_mutating_command_previews_the_command_and_the_reason(ctx: ToolContext):
    diff = preview_of(TOOLS["bash"], {"command": "rm -rf build"}, ctx)
    assert diff is not None
    assert "rm -rf build" in diff
    assert "requires approval" in diff
    assert "no checkpoint is possible" in diff, "the user must know undo cannot cover this"


def test_the_preview_includes_the_stated_purpose(ctx: ToolContext):
    diff = preview_of(
        TOOLS["bash"],
        {"command": "rm -rf build", "description": "clear the build cache"},
        ctx,
    )
    assert "clear the build cache" in diff


def test_the_checkpoint_limitation_is_recorded(ctx: ToolContext):
    result = run("echo x", ctx)
    assert result.meta["checkpoint"] == "not-possible"


def test_bash_is_exposed_to_the_model():
    spec = BashTool().spec()
    assert spec.name == "bash"
    assert "approval" in spec.description
    assert "stdin is closed" in spec.description
