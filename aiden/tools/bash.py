"""``bash`` — run a shell command, bounded in every direction that matters.

Three bounds, each because the alternative hangs or floods the run:

- **Time.** A default 30 s and a hard 300 s ceiling, and the timeout kills the *process group*: a
  shell that leaves children running is worse than no timeout at all.
- **Output.** 16 KB with head *and* tail elision. `research/02` §2 measured one unbounded search at
  ~90k tokens; mini-SWE-agent elides with head/tail rather than truncating the tail, because the end
  of a command's output is usually where the result is.
- **Input.** ``stdin`` is closed. An interactive subprocess hangs the run, and `research/02` §7 is
  explicit that interactive commands need background+poll, which this milestone does not have.
  Failing fast with "the command asked for input" is the honest behaviour.

Sandboxing: **none in-process**, deliberately. `research/02` §8 and pi's security doc both conclude
that partial isolation gets mistaken for a boundary while still depending on the host shell and
credentials. The honest controls are the confined working directory, the timeout, the output budget,
and the approval gate — with real isolation being a container's job.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from typing import Any

from .. import config
from ..providers.types import ToolSpec
from .command_policy import CommandPolicy
from .output import cap_text, spill
from .types import ToolContext, ToolResult

DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 300

#: Elide with a head and a tail rather than truncating, because the tail is usually the answer.
HEAD_LINES = 120
TAIL_LINES = 80


class BashTool:
    name = "bash"
    #: A shell can change anything, so it always goes through the gate. Whether it needs *approval*
    #: is decided per call by the command policy.
    mutating = True

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="bash",
            description=(
                "Run a shell command in the project directory. Read-only commands (ls, cat, grep, "
                "git status/diff/log, pytest --collect-only) run immediately; anything else awaits "
                "the user's approval, and any command using a pipe, redirect or `&&` is treated as "
                "unprovable and asks. Output is capped with a head and tail; stdin is closed, so do "
                "not run interactive commands. Set a longer timeout for slow commands."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                    "timeout": {
                        "type": "integer",
                        "description": f"Seconds before the command is killed (default "
                        f"{DEFAULT_TIMEOUT_S}, max {MAX_TIMEOUT_S}).",
                    },
                    "description": {
                        "type": "string",
                        "description": "What this command is for, shown in the approval prompt.",
                    },
                },
                "required": ["command"],
            },
        )

    # ------------------------------------------------------------------ gate

    def approval_required(self, args: dict[str, Any], ctx: ToolContext) -> bool:
        """Read-only commands do not need approval; everything unprovable does."""
        command = str(args.get("command", ""))
        return not (ctx.command_policy or CommandPolicy()).is_read_only(command)

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        """The ``+``-style block shown for approval: the command, and why it is not provable.

        There is no diff to show — we cannot know what a shell command will touch, and pretending
        otherwise would be worse than saying so.
        """
        command = str(args.get("command", ""))
        if not command.strip():
            return None
        policy = ctx.command_policy or CommandPolicy()
        if policy.is_read_only(command):
            return None
        reason = policy.reason(command)
        described = str(args.get("description", "")).strip()
        lines = [f"+ $ {command}"]
        if described:
            lines.insert(0, f"# {described}")
        lines.append("")
        lines.append(f"# requires approval: {reason}")
        lines.append(
            "# no checkpoint is possible for a shell command: we cannot know which files it"
        )
        lines.append("# will touch. git is the safety net for this class of change.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ run

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args.get("command", ""))
        if not command.strip():
            return ToolResult.error("command is required")

        timeout = _timeout(args.get("timeout"))
        cwd = ctx.cwd

        started = time.perf_counter()
        try:
            proc = subprocess.Popen(  # noqa: S602 - the whole point of the tool is a shell
                command,
                shell=True,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,  # interactive commands must fail fast, not hang
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                # Own process group so a timeout can kill the whole tree, not just the shell.
                start_new_session=True,
                env=_child_env(),
            )
        except OSError as exc:
            return ToolResult.error(f"could not start the command: {exc}")

        timed_out = False
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            try:
                output, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                output = ""
        duration_ms = int((time.perf_counter() - started) * 1000)

        output = output or ""
        exit_code = proc.returncode

        capped = _elide(output, ctx)
        header = f"exit {exit_code} · {duration_ms}ms"
        if timed_out:
            header = (
                f"killed after {timeout}s (exit {exit_code}) · the command did not finish; "
                f"raise `timeout` if it legitimately needs longer"
            )
        body = capped.text
        result = ToolResult(
            output=f"{header}\n{body}" if body else header,
            is_error=bool(exit_code) or timed_out,
            truncated=capped.truncated,
            hint=capped.hint,
            meta={
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "timed_out": timed_out,
                "command": command,
                "checkpoint": "not-possible",
            },
        )
        return result


# --------------------------------------------------------------------------- internals


def _timeout(raw: Any) -> int:
    value = DEFAULT_TIMEOUT_S
    if isinstance(raw, (int, float, str)):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = DEFAULT_TIMEOUT_S
    return max(1, min(value, MAX_TIMEOUT_S))


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the process group, then the process, so nothing survives the timeout."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            proc.kill()


def _child_env() -> dict[str, str]:
    """Environment for the child process.

    Passed through, because tools need `PATH` and git needs its config. Credentials are *not*
    stripped — that would break git remotes — which is another reason real isolation belongs in a
    container rather than in this process.
    """
    env = dict(os.environ)
    env.setdefault("GIT_PAGER", "cat")  # a pager would block on a closed stdin
    env.setdefault("PAGER", "cat")
    env["AIDEN_NONINTERACTIVE"] = "1"
    return env


def _elide(output: str, ctx: ToolContext):
    """Head + tail elision, then spill when the whole thing is very large."""
    lines = output.splitlines()
    budget_bytes = config.READ_MAX_BYTES

    if len(lines) <= HEAD_LINES + TAIL_LINES and len(output.encode()) <= budget_bytes:
        return cap_text(output, max_bytes=budget_bytes, max_lines=10_000)

    if len(lines) > HEAD_LINES + TAIL_LINES:
        head = lines[:HEAD_LINES]
        tail = lines[-TAIL_LINES:]
        hidden = len(lines) - HEAD_LINES - TAIL_LINES
        joined = "\n".join(
            [
                *head,
                f"… {hidden} lines elided …",
                *tail,
            ]
        )
    else:
        # Few lines but many bytes: cut inside the text rather than dropping lines.
        joined = output[:budget_bytes]

    capped = cap_text(joined, max_bytes=budget_bytes, max_lines=10_000)
    hint = f"[elided: showed {HEAD_LINES} head and {TAIL_LINES} tail lines of {len(lines)} total"
    if len(output.encode()) > budget_bytes * 4:
        path = spill(output, name="bash", directory=ctx.spill_dir)
        hint += f"; the full output is at {path}"
    hint += "]"

    from .output import Capped

    return Capped(
        text=capped.text,
        truncated=True,
        hint=hint,
        total_bytes=len(output.encode()),
        total_lines=len(lines),
    )
