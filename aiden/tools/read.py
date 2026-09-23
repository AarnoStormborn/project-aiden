"""``read`` — paged, budgeted file access.

research/02 §2: the whole-file ratio measured 171x on this repo, so the ceiling matters more
than the interface. 16 KB / 400 lines, cut on line boundaries, with an explicit
``offset=N`` continuation hint.
"""

from __future__ import annotations

from typing import Any

from .. import config
from ..providers.types import ToolSpec
from .output import cap_text
from .types import ToolContext, ToolResult, looks_binary, resolve_in_cwd

DEFAULT_LIMIT = config.READ_MAX_LINES


class ReadTool:
    name = "read"

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="read",
            description=(
                "Read a text file inside the working directory. Returns numbered lines. "
                "Output is capped; if it is truncated you will get an offset to continue from. "
                "Binary files are refused. Paths outside the working directory are refused."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the working directory.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-based line number to start from. Use the offset from "
                        "a truncation hint to continue reading.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Maximum lines to return (default {DEFAULT_LIMIT}).",
                    },
                },
                "required": ["path"],
            },
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_path = str(args.get("path", ""))
        path, refusal = resolve_in_cwd(raw_path, ctx.cwd)
        if path is None:
            return ToolResult.error(refusal)

        if not path.exists():
            return ToolResult.error(f"no such file: {raw_path}")
        if path.is_dir():
            listing = _list_dir(path, ctx.cwd)
            return ToolResult(
                output=f"{raw_path} is a directory. Entries:\n{listing}",
                meta={"kind": "directory"},
            )

        try:
            data = path.read_bytes()
        except OSError as exc:
            return ToolResult.error(f"cannot read {raw_path}: {exc}")

        if looks_binary(data):
            return ToolResult.error(
                f"refused: {raw_path} looks like a binary file ({len(data)} bytes). "
                "Only text files can be read."
            )

        text = data.decode("utf-8", "replace")
        ctx.read_state.record(path, text)

        offset = _as_int(args.get("offset"), 1, minimum=1)
        limit = _as_int(args.get("limit"), DEFAULT_LIMIT, minimum=1)

        lines = text.splitlines()
        total = len(lines)
        if offset > total:
            return ToolResult.error(
                f"offset {offset} is past the end of {raw_path} ({total} lines)."
            )

        window = "\n".join(lines[offset - 1 : offset - 1 + limit])
        capped = cap_text(
            window,
            max_bytes=config.READ_MAX_BYTES,
            max_lines=limit,
            offset=offset - 1,
        )

        numbered = _number(capped.text, start=offset)

        # "truncated" means the model did not receive the whole file, whether because a budget
        # cut this window or because the file simply continues. Either way it must be told, and
        # told where to resume from (research/02 §3: caps leave traces).
        kept_lines = capped.text.splitlines()
        next_offset = offset + len(kept_lines)
        remaining = total - (next_offset - 1)
        truncated = capped.truncated or remaining > 0

        hint = capped.hint
        if not capped.truncated and remaining > 0:
            hint = (
                f"[showing lines {offset}-{next_offset - 1} of {total}. "
                f"Call read again with offset={next_offset} for the next chunk.]"
            )

        return ToolResult(
            output=numbered,
            truncated=truncated,
            hint=hint,
            meta={"path": str(path), "lines": total, "offset": offset, "remaining": remaining},
        )


def _as_int(value: Any, default: int, *, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _number(text: str, *, start: int) -> str:
    """Number lines so the model can cite them and re-request exact regions."""
    return "\n".join(f"{start + i}\t{line}" for i, line in enumerate(text.splitlines()))


def _list_dir(path, cwd) -> str:
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
    return "\n".join(entries[: config.GLOB_MAX_PATHS])
