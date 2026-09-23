"""``grep`` — ripgrep-first search with an explicit context opt-in.

Two measured facts drive the interface (research/02 §§3,5):

- ``rg`` was 6.3x faster than ``grep -rn`` here (0.175 s vs 1.094 s over 7,386 files).
- The default must be cheap: a bare ``-n`` and ``-C 3`` differ by **6.7x** tokens for the
  same 31 hits, and a context-free ``rg`` on a large corpus hit ~90k tokens. So context is
  opt-in, and the default mode returns *which files matched* rather than their contents.

A pure-Python fallback keeps the tool working (and the tests honest) when ``rg`` is absent.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .. import config
from ..providers.types import ToolSpec
from .output import cap_with_spill
from .types import ToolContext, ToolResult, resolve_in_cwd

MODE_FILES = "files-first"
MODE_CONTENT = "content"
MODES = (MODE_FILES, MODE_CONTENT)

#: Directories that never carry signal for a question about the code.
#: ``.aiden-research`` and ``.pi`` are local scratch/tool config, not project content.
#: ``.github`` is deliberately *not* skipped: CI config is legitimate project content.
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".aiden",
    ".aiden-research",
    ".pi",
}

RG_TIMEOUT_S = 20


class GrepTool:
    name = "grep"

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="grep",
            description=(
                "Search file contents in the working directory with a regular expression "
                "(ripgrep syntax). "
                f"mode='{MODE_FILES}' (default) returns matching file paths with a match count "
                "and is by far the cheapest option — use it first, then read the file. "
                f"mode='{MODE_CONTENT}' returns matching lines as file:line:text, capped. "
                "Pass context>0 only when you specifically need surrounding lines: it "
                "multiplies the token cost substantially."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regular expression to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory to search, relative to the working "
                        "directory. Defaults to the whole working directory.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": list(MODES),
                        "description": f"'{MODE_FILES}' (paths only, default) or '{MODE_CONTENT}' (lines).",
                    },
                    "context": {
                        "type": "integer",
                        "description": "Lines of context around each match. 0 by default; "
                        "expensive, so only use it when you need it.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"Cap on returned hits (default {config.GREP_MAX_HITS}).",
                    },
                },
                "required": ["pattern"],
            },
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args.get("pattern", ""))
        if not pattern:
            return ToolResult.error("pattern is required")

        mode = str(args.get("mode") or MODE_FILES)
        if mode not in MODES:
            return ToolResult.error(f"unknown mode '{mode}'. Use one of: {', '.join(MODES)}.")

        search_root = ctx.cwd
        raw_path = args.get("path")
        if raw_path:
            resolved, refusal = resolve_in_cwd(str(raw_path), ctx.cwd)
            if resolved is None:
                return ToolResult.error(refusal)
            if not resolved.exists():
                return ToolResult.error(f"no such path: {raw_path}")
            search_root = resolved

        context = _as_int(args.get("context"), 0)
        max_results = _as_int(args.get("max_results"), config.GREP_MAX_HITS)
        if context < 0:
            context = 0
        if context > 0 and mode == MODE_FILES:
            # Context is meaningless without content; honour the request rather than ignore it.
            mode = MODE_CONTENT

        # An invalid regex is a user error, not a crash: compile first, report cleanly.
        try:
            re.compile(pattern)
        except re.error as exc:
            if not shutil.which("rg"):
                return ToolResult.error(f"invalid regular expression: {exc}")
            # rg has slightly different syntax; let rg be the judge when it is available.

        rg = shutil.which("rg")
        if rg:
            result = _run_rg(rg, pattern, search_root, mode, context, max_results, base=ctx.cwd)
        else:
            result = _run_python(pattern, search_root, mode, context, max_results, base=ctx.cwd)
        return _apply_byte_budget(result, ctx)


# --------------------------------------------------------------------------- byte budget


def _apply_byte_budget(result: ToolResult, ctx: ToolContext) -> ToolResult:
    """Enforce a *byte* budget on grep output, spilling the full text when very large.

    The hit cap alone is not enough: 200 matches inside minified files or long lines can still
    be hundreds of kilobytes. research/02 §3 measured one unbounded search at ~90k tokens, so
    every tool needs a byte ceiling as well as a count ceiling.
    """
    budget = config.GREP_MAX_BYTES
    if len(result.output.encode("utf-8")) <= budget:
        return result

    capped = cap_with_spill(result.output, name="grep", directory=ctx.spill_dir, max_bytes=budget)
    hints = [h for h in (result.hint, capped.hint) if h]
    return ToolResult(
        output=capped.text,
        is_error=result.is_error,
        truncated=True,
        hint="\n".join(hints),
        meta={**result.meta, "bytes_capped": True, "bytes": capped.total_bytes},
    )


# --------------------------------------------------------------------------- ripgrep path


def _run_rg(
    rg: str,
    pattern: str,
    root: Path,
    mode: str,
    context: int,
    max_results: int,
    *,
    base: Path,
) -> ToolResult:
    cmd = [rg, "--no-config", "--color=never", "--no-heading", "--with-filename"]
    if mode == MODE_FILES:
        cmd += ["--count-matches"]
    else:
        cmd += ["--line-number"]
        if context:
            cmd += ["-C", str(context)]
    for skipped in sorted(SKIP_DIRS):
        # '!**/dir/**' rather than '!dir/**': ripgrep matches globs against the path as given,
        # so an absolute search root silently ignores an unanchored '!dir/**' exclusion.
        cmd += ["--glob", f"!**/{skipped}/**"]
    cmd += ["--regexp", pattern, str(root)]

    try:
        proc = subprocess.run(  # noqa: S603 - argv built here, no shell, no user input
            cmd,
            capture_output=True,
            text=True,
            timeout=RG_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult.error(
            f"search timed out after {RG_TIMEOUT_S}s; narrow the pattern or path"
        )
    except OSError as exc:
        return ToolResult.error(f"search failed: {exc}")

    # rg exits 1 for "no matches", which is a normal outcome, not an error.
    if proc.returncode not in (0, 1):
        detail = (proc.stderr or "").strip().splitlines()
        message = detail[0] if detail else f"rg exited {proc.returncode}"
        return ToolResult.error(f"search failed: {message}")

    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        return _no_matches(pattern, root, mode)
    return _format(lines, base, mode, max_results)


# --------------------------------------------------------------------------- fallback path


def _run_python(
    pattern: str,
    root: Path,
    mode: str,
    context: int,
    max_results: int,
    *,
    base: Path,
) -> ToolResult:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolResult.error(f"invalid regular expression: {exc}")

    hits: list[str] = []
    files = [root] if root.is_file() else _walk(root)

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        matched = [i for i, line in enumerate(lines) if regex.search(line)]
        if not matched:
            continue
        rel = _relative(path, base)
        if mode == MODE_FILES:
            hits.append(f"{rel}:{len(matched)}")
        else:
            for index in matched:
                if context:
                    start = max(0, index - context)
                    end = min(len(lines), index + context + 1)
                    for offset in range(start, end):
                        hits.append(f"{rel}:{offset + 1}:{lines[offset]}")
                else:
                    hits.append(f"{rel}:{index + 1}:{lines[index]}")
        if len(hits) >= max_results:
            break

    if not hits:
        return _no_matches(pattern, root, mode)
    return _format(hits, base, mode, max_results)


def _walk(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            out.append(path)
    return sorted(out)


# --------------------------------------------------------------------------- formatting


def _format(lines: list[str], base: Path, mode: str, max_results: int) -> ToolResult:
    total = len(lines)
    kept = lines[:max_results]
    # Paths relative to the *working directory*, not the search root. Two reasons: relative paths
    # are what the model must pass back to `read`, and an absolute path is long enough to wrap
    # across lines in a narrow terminal. Stripping the search root instead was wrong whenever the
    # root was a file, because ripgrep then emits "<file>:<line>:..." with no trailing separator.
    base_prefix = str(base) + "/"
    kept = [ln.replace(base_prefix, "") for ln in kept]

    truncated = total > max_results
    hint = ""
    if truncated:
        hint = (
            f"[truncated: showing {max_results} of {total} matches. "
            "Narrow the pattern, or use mode='files-first' to see which files matched first.]"
        )

    label = (
        "file(s) with matches (path:count)" if mode == MODE_FILES else "matches (path:line:text)"
    )
    return ToolResult(
        output=f"{len(kept)} {label}:\n" + "\n".join(kept),
        truncated=truncated,
        hint=hint,
        meta={"matches": total, "returned": len(kept), "mode": mode},
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root if root.is_dir() else root.parent))
    except ValueError:
        return path.name


def _no_matches(pattern: str, root: Path, mode: str) -> ToolResult:
    """An explicit empty result — a bare "" reads as failure and derails the model."""
    where = "." if root == root.parent else str(root)
    return ToolResult(
        output=f"no matches for /{pattern}/ in {where} (mode={mode}). "
        "The search ran successfully; there is nothing to show.",
        meta={"matches": 0, "mode": mode},
    )


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
