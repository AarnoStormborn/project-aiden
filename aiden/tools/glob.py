"""``glob`` — find files by path pattern, cheaply.

research/02 §2 lists this as rung 1 of the retrieval ladder ("which files exist? ~30 ms,
<100 tokens"). It is the cheapest tool, so it is the one the prompt tells the model to try
first when it does not know where something lives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import config
from ..providers.types import ToolSpec
from .grep import SKIP_DIRS
from .types import ToolContext, ToolResult


class GlobTool:
    name = "glob"

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="glob",
            description=(
                "Find files by path pattern, e.g. '**/*.py' or 'docs/**/*.md'. Returns paths "
                "sorted, capped in number. Use this first to discover where something lives."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern relative to the working directory.",
                    },
                    "max_paths": {
                        "type": "integer",
                        "description": f"Cap on returned paths (default {config.GLOB_MAX_PATHS}).",
                    },
                },
                "required": ["pattern"],
            },
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return ToolResult.error("pattern is required")
        if pattern.startswith("/") or ".." in Path(pattern).parts:
            return ToolResult.error(
                f"refused: pattern '{pattern}' must be relative to the working directory."
            )

        max_paths = _as_int(args.get("max_paths"), config.GLOB_MAX_PATHS)
        root = ctx.cwd

        matches: list[str] = []
        for path in sorted(root.glob(pattern)):
            if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            if not path.is_file():
                continue
            matches.append(str(path.relative_to(root)))

        if not matches:
            return ToolResult(
                output=(
                    f"no files match '{pattern}' in {root}. "
                    "The search ran successfully; there is nothing to show."
                ),
                meta={"matches": 0},
            )

        truncated = len(matches) > max_paths
        kept = matches[:max_paths]
        hint = ""
        if truncated:
            hint = (
                f"[truncated: showing {max_paths} of {len(matches)} paths. "
                "Use a more specific pattern.]"
            )
        return ToolResult(
            output=f"{len(kept)} path(s) matching '{pattern}':\n" + "\n".join(kept),
            truncated=truncated,
            hint=hint,
            meta={"matches": len(matches), "returned": len(kept)},
        )


def _as_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default
