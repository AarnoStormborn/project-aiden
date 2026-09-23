"""``edit`` — exact-string replacement, with the guards that make it safe.

`research/02` §4 chose this format on measurement, not taste: model-generated git-style unified
diffs applied on only **48.7%** of GPT-4 "oracle-collapsed" attempts (1,116/2,292), and 684 of those
only after an automatic repair pass. On aider's edit leaderboard, strong models emit SEARCH/REPLACE
with **99.2%** format compliance for 84.2% correct completions.

Four guards, in the order `research/01` §permissions specifies. Each returns an **error result**
rather than raising, so the model sees the failure and can correct it:

1. the file must have been read this session, and its hash must be unchanged since (hashing beats
   mtime — `research/02` §6 measured 2.2 GB/s);
2. ``old_string`` must match exactly once, unless ``replace_all`` is set;
3. the path must pass the write policy;
4. after applying, the result must still parse, and a failure is reported back.

A whitespace-tolerant retry exists because indentation mismatches are the most common benign
failure; it is reported when used, so the model learns what its input looked like.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from ..providers.types import ToolSpec
from .policy import WritePolicy
from .types import ToolContext, ToolResult, looks_binary, resolve_in_cwd

#: Guard for the tolerant retry: below this similarity we do not guess.
SIMILARITY_FLOOR = 0.80


class EditTool:
    name = "edit"
    mutating = True

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="edit",
            description=(
                "Replace an exact string in a file. Read the file first: an edit is refused if the "
                "file has not been read this session, or if it changed since you read it. "
                "old_string must appear exactly once unless replace_all is true, so include enough "
                "surrounding context to be unique. Prefer this over rewriting a whole file. "
                "Every edit needs the user's approval, so say why you are making it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File to edit, relative to the project.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact text to replace, unique in the file.",
                    },
                    "new_string": {"type": "string", "description": "The replacement text."},
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence. Only for genuinely repeated text.",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_path = str(args.get("path", ""))
        old = args.get("old_string")
        new = args.get("new_string")
        replace_all = bool(args.get("replace_all", False))

        if not isinstance(old, str) or not isinstance(new, str):
            return ToolResult.error("old_string and new_string must both be strings")
        if old == new:
            return ToolResult.error("old_string and new_string are identical; nothing to do")
        if not old:
            return ToolResult.error(
                "old_string is empty. To create a file use write; to add text, include the "
                "surrounding lines you want it inserted next to."
            )

        path, refusal = resolve_in_cwd(raw_path, ctx.cwd)
        if path is None:
            return ToolResult.error(refusal)
        if not path.is_file():
            return ToolResult.error(f"no such file: {raw_path}. To create it, use write.")

        denied = self._check_policy(path, raw_path, ctx)
        if denied is not None:
            return denied

        try:
            data = path.read_bytes()
        except OSError as exc:
            return ToolResult.error(f"cannot read {raw_path}: {exc}")
        if looks_binary(data):
            return ToolResult.error(f"refused: {raw_path} is a binary file")

        text = data.decode("utf-8", "replace")

        # Guard 1: read-before-edit, enforced rather than merely recorded.
        if str(path) not in ctx.read_state.hashes:
            return ToolResult.error(
                f"{raw_path} has not been read in this session. Read it first so the edit is "
                "based on what is actually there."
            )
        if ctx.read_state.stale(path, text):
            return ToolResult.error(
                f"{raw_path} changed since you read it. Read it again before editing, or your "
                "replacement may not match what is on disk."
            )

        # Guard 2: the match.
        occurrences = text.count(old)
        tolerant_note = ""
        if occurrences == 0:
            recovered = _whitespace_tolerant_find(text, old)
            if recovered is None:
                return self._no_match_result(raw_path, text, old)
            old = recovered
            occurrences = text.count(old)
            tolerant_note = " (matched with whitespace differences ignored; check the result)"
        if occurrences > 1 and not replace_all:
            lines = _line_numbers(text, old)
            return ToolResult.error(
                f"refused: old_string appears {occurrences} times in {raw_path} "
                f"(lines {', '.join(str(n) for n in lines[:8])}). Include more surrounding "
                "context to make it unique, or pass replace_all=true if you mean all of them."
            )

        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)

        # Guard 4: the result must still parse, where we can tell.
        problem = _validate(path, updated)
        try:
            path.write_bytes(updated.encode("utf-8"))
        except OSError as exc:
            return ToolResult.error(f"cannot write {raw_path}: {exc}")

        ctx.read_state.record(path, updated)
        removed = len(old.splitlines())
        added = len(new.splitlines())
        summary = (
            f"edited {raw_path}: {occurrences if replace_all else 1} replacement(s), "
            f"-{removed} +{added} lines{tolerant_note}"
        )
        if problem:
            # Kept, not reverted: the error is the information the model needs to fix it, and a
            # silent revert would hide what it actually produced.
            return ToolResult(
                output=f"{summary}\n\nwarning: the file no longer parses:\n{problem}",
                is_error=True,
                meta={"path": str(path), "applied": True, "invalid": True},
            )
        return ToolResult(
            output=summary, meta={"path": str(path), "applied": True, "replacements": occurrences}
        )

    # ------------------------------------------------------------------ preview

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        """The diff this edit would produce, or ``None`` when it would be refused.

        Returning ``None`` for a doomed edit matters: there is no point asking a human to approve a
        change the tool is about to reject anyway.
        """
        from ..diffutil import unified

        raw_path = str(args.get("path", ""))
        old = args.get("old_string")
        new = args.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            return None

        path, _refusal = resolve_in_cwd(raw_path, ctx.cwd)
        if path is None or not path.is_file():
            return None
        _resolved, decision = (ctx.write_policy or WritePolicy()).check(raw_path, ctx.cwd)
        if not decision.allowed:
            return None
        if str(path) not in ctx.read_state.hashes:
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if ctx.read_state.stale(path, text):
            return None

        occurrences = text.count(old)
        candidate = old
        if occurrences == 0:
            recovered = _whitespace_tolerant_find(text, old)
            if recovered is None:
                return None
            candidate = recovered
            occurrences = text.count(candidate)
        if occurrences > 1 and not bool(args.get("replace_all", False)):
            return None

        updated = (
            text.replace(candidate, new)
            if args.get("replace_all")
            else text.replace(candidate, new, 1)
        )
        return unified(text, updated, raw_path)

    # ------------------------------------------------------------------ helpers

    def _check_policy(self, path: Path, raw_path: str, ctx: ToolContext) -> ToolResult | None:
        policy = ctx.write_policy or WritePolicy()
        _resolved, decision = policy.check(raw_path, ctx.cwd)
        if not decision.allowed:
            return ToolResult.error(decision.reason)
        return None

    def _no_match_result(self, raw_path: str, text: str, old: str) -> ToolResult:
        hint = _closest_region(text, old)
        message = f"old_string was not found in {raw_path}."
        if hint:
            message += f"\n\nClosest text in the file:\n{hint}"
        message += "\n\nRead the file and copy the exact text, including indentation."
        return ToolResult.error(message)


def _line_numbers(text: str, needle: str) -> list[int]:
    out: list[int] = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index == -1:
            return out
        out.append(text.count("\n", 0, index) + 1)
        start = index + 1


def _whitespace_tolerant_find(text: str, old: str) -> str | None:
    """Find a region matching ``old`` ignoring leading/trailing whitespace per line."""
    wanted = [line.strip() for line in old.splitlines()]
    if not wanted:
        return None
    lines = text.splitlines(keepends=True)
    width = len(wanted)
    for index in range(len(lines) - width + 1):
        window = lines[index : index + width]
        if [line.strip() for line in window] == wanted:
            return "".join(window)
    return None


def _closest_region(text: str, old: str, *, window: int = 12) -> str:
    """The most similar slice of the file, for a 'did you mean' hint."""
    lines = text.splitlines()
    if not lines:
        return ""
    best_ratio = 0.0
    best_start = 0
    span = max(1, min(len(old.splitlines()), len(lines)))
    for index in range(max(1, len(lines) - span + 1)):
        candidate = "\n".join(lines[index : index + span])
        ratio = difflib.SequenceMatcher(None, candidate, old).ratio()
        if ratio > best_ratio:
            best_ratio, best_start = ratio, index
    if best_ratio < 0.3:
        return ""
    snippet = lines[best_start : best_start + span][:window]
    return "\n".join(f"{best_start + i + 1}\t{line}" for i, line in enumerate(snippet))


def _validate(path: Path, content: str) -> str:
    """Parse-check the result where we have a parser. Empty string means fine."""
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
