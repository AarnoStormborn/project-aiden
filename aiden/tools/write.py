"""``write`` — whole-file creation and replacement.

Kept narrow on purpose. `research/02` §4 keeps whole-file writes as the *fallback* for new and small
files, not the default: on aider's leaderboard `gemini-exp-1206` scored 80.5% on ``whole`` against
69.2% on ``diff``, but a whole-file write of a large file is a large diff to review and a large
approval prompt to read, and it silently discards anything the model did not know about.

So: creating a file is fine, overwriting a file requires having read it, and overwriting a file with
a large body is refused with a pointer to ``edit``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..providers.types import ToolSpec
from .policy import WritePolicy
from .types import ToolContext, ToolResult, looks_binary, resolve_in_cwd

#: Above this many lines, an overwrite should be a targeted edit instead. Not a hard rule about
#: correctness — a rule about what a human can meaningfully approve.
OVERWRITE_LINE_LIMIT = 200


class WriteTool:
    name = "write"
    mutating = True

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="write",
            description=(
                "Create a file, or replace one you have read. Use this for new files. To change "
                "part of a large existing file, use edit instead: a whole-file write would discard "
                "everything you did not include. Every write needs the user's approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File to write, relative to the project.",
                    },
                    "content": {"type": "string", "description": "The complete file contents."},
                },
                "required": ["path", "content"],
            },
        )

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        """The diff this write would produce, or ``None`` when it would be refused."""
        from ..diffutil import unified

        raw_path = str(args.get("path", ""))
        content = args.get("content")
        if not isinstance(content, str):
            return None
        path, _refusal = resolve_in_cwd(raw_path, ctx.cwd)
        if path is None or path.is_dir():
            return None
        _resolved, decision = (ctx.write_policy or WritePolicy()).check(raw_path, ctx.cwd)
        if not decision.allowed:
            return None
        if not path.exists():
            return unified("", content, raw_path)
        if str(path) not in ctx.read_state.hashes:
            return None
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if ctx.read_state.stale(path, existing):
            return None
        return unified(existing, content, raw_path)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_path = str(args.get("path", ""))
        content = args.get("content")
        if not isinstance(content, str):
            return ToolResult.error("content must be a string")

        path, refusal = resolve_in_cwd(raw_path, ctx.cwd)
        if path is None:
            return ToolResult.error(refusal)
        if path.is_dir():
            return ToolResult.error(f"{raw_path} is a directory")

        policy = ctx.write_policy or WritePolicy()
        _resolved, decision = policy.check(raw_path, ctx.cwd)
        if not decision.allowed:
            return ToolResult.error(decision.reason)

        exists = path.exists()
        if exists:
            try:
                data = path.read_bytes()
            except OSError as exc:
                return ToolResult.error(f"cannot read {raw_path}: {exc}")
            if looks_binary(data):
                return ToolResult.error(f"refused: {raw_path} is a binary file")
            if str(path) not in ctx.read_state.hashes:
                return ToolResult.error(
                    f"{raw_path} already exists and has not been read this session. Read it first, "
                    "or use edit to change part of it."
                )
            if ctx.read_state.stale(path, data.decode("utf-8", "replace")):
                return ToolResult.error(
                    f"{raw_path} changed since you read it. Read it again before overwriting it."
                )
            lines = len(data.decode("utf-8", "replace").splitlines())
            if lines > OVERWRITE_LINE_LIMIT and not decision.sensitive:
                return ToolResult.error(
                    f"refused: {raw_path} has {lines} lines; a whole-file overwrite would be a "
                    "very large change to review. Use edit for the parts you are changing."
                )

        problem = _validate(path, content)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult.error(f"cannot write {raw_path}: {exc}")

        ctx.read_state.record(path, content)
        action = "overwrote" if exists else "created"
        summary = f"{action} {raw_path} ({len(content.splitlines())} lines)"
        if problem:
            return ToolResult(
                output=f"{summary}\n\nwarning: the file does not parse:\n{problem}",
                is_error=True,
                meta={"path": str(path), "applied": True, "invalid": True},
            )
        return ToolResult(
            output=summary, meta={"path": str(path), "applied": True, "created": not exists}
        )


def _validate(path: Path, content: str) -> str:
    """Same parse gate as ``edit``: a broken file the model can see is a correction."""
    if path.suffix == ".py":
        try:
            compile(content, str(path), "exec")
        except SyntaxError as exc:
            location = f"line {exc.lineno}" if exc.lineno else "unknown line"
            return f"{exc.__class__.__name__}: {exc.msg} ({location})"
        return ""
    if path.suffix == ".json":
        import json

        try:
            json.loads(content)
        except ValueError as exc:
            return f"invalid JSON: {exc}"
        return ""
    return ""
