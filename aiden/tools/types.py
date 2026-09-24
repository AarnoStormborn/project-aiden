"""Tool contract: results are values, budgets are explicit, paths are confined.

Design rules straight from the research:

- **Never raise.** Every guard class returns an error *result* the model can read and correct
  (docs/research/01-harness-anatomy.md §validation: "each class of failure returns an error
  tool result, never raises, so the model can see and correct it").
- **Empty output must be explicit.** A bare empty string reads as failure and derails agents
  ([r01] §shaping), so "no matches" is a message, not "".
- **Caps leave traces.** Truncated output carries a continuation hint ([r02] §3).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..providers.types import ToolSpec


@dataclass(slots=True)
class ToolResult:
    output: str
    is_error: bool = False
    truncated: bool = False
    hint: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.is_error

    def render(self) -> str:
        """What the model actually sees: output plus any continuation hint."""
        if self.hint:
            return f"{self.output}\n\n{self.hint}" if self.output else self.hint
        return self.output

    @classmethod
    def error(cls, message: str, **meta: Any) -> ToolResult:
        return cls(output=message, is_error=True, meta=meta)


@dataclass
class ReadState:
    """Content hashes of files the agent has read this session.

    Read-before-edit plus stale-view rejection is the standard guard in shipped harnesses
    ([r02] §6), and hashing beats mtime because mtime lies. v0.1 is read-only, so this is
    recorded but not yet enforced — it exists so the edit tool in v0.2 has a place to look,
    and so the hash survives a future resume.
    """

    hashes: dict[str, str] = field(default_factory=dict)

    def record(self, path: Path, text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        self.hashes[str(path)] = digest
        return digest

    def stale(self, path: Path, text: str) -> bool:
        previous = self.hashes.get(str(path))
        if previous is None:
            return False
        return previous != hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class ToolContext:
    """Everything a tool is allowed to know about the run."""

    cwd: Path
    read_state: ReadState = field(default_factory=ReadState)
    spill_dir: Path | None = None
    #: Path rules for mutating tools. Imported lazily to avoid a cycle at module load.
    write_policy: Any = None
    #: Rules for what a shell command may do unattended.
    command_policy: Any = None
    #: Rules for what may be fetched over the network.
    fetch_policy: Any = None
    #: Injected HTTP client, so tests never touch the network.
    http_client: Any = None
    #: URL -> extracted text, for this session only. Re-fetching a page the run already read is the
    #: most expensive kind of waste a tool can commit.
    fetch_cache: dict[str, str] = field(default_factory=dict)

    def policy(self) -> Any:
        if self.write_policy is None:
            from .policy import WritePolicy

            self.write_policy = WritePolicy.from_env()
        return self.write_policy

    def commands(self) -> Any:
        if self.command_policy is None:
            from .command_policy import CommandPolicy

            self.command_policy = CommandPolicy()
        return self.command_policy


class Tool(Protocol):
    """A tool the model can call."""

    name: str

    #: True when the tool can change the working tree. Mutating tools are gated by policy and
    #: approval.
    mutating: bool

    # Mutating tools additionally define ``preview(args, ctx) -> str | None``, returning the diff a
    # call would produce so approval can show it. It is deliberately *not* a Protocol member: a
    # read-only tool has nothing to preview, and forcing every tool to declare a no-op would be
    # noise. Callers use ``preview_of()`` below, which returns None when there is none.

    def spec(self) -> ToolSpec: ...

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


# --------------------------------------------------------------------------- path guard


def preview_of(tool: Tool, args: dict[str, Any], ctx: ToolContext) -> str | None:
    """What ``tool`` would do for ``args``, or ``None`` when there is nothing to show.

    Deliberately *not* restricted to mutating tools: ``web_fetch`` changes nothing locally and still
    needs the user to see the URL it is about to send.
    """
    preview = getattr(tool, "preview", None)
    if preview is None:
        return None
    try:
        return preview(args, ctx)
    except Exception:
        # A preview that raises must not block a legitimate change, and must not fake one either:
        # returning None means "no preview", and the tool's own guards still apply.
        return None


def resolve_in_cwd(raw: str, cwd: Path) -> tuple[Path | None, str]:
    """Resolve ``raw`` relative to ``cwd``, refusing anything that escapes it.

    Returns ``(path, "")`` on success or ``(None, reason)`` on refusal. Symlinks are
    resolved before the check, so a link pointing outside the tree is refused too.
    """
    if not raw or not raw.strip():
        return None, "path is empty"
    candidate = Path(raw)
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (cwd / candidate).resolve()
    except (OSError, RuntimeError) as exc:  # RuntimeError: symlink loop
        return None, f"cannot resolve path '{raw}': {exc}"

    try:
        resolved.relative_to(cwd.resolve())
    except ValueError:
        return None, (
            f"refused: '{raw}' resolves to {resolved}, which is outside the working "
            f"directory {cwd}. Only paths inside the project can be read."
        )
    return resolved, ""


def looks_binary(data: bytes) -> bool:
    """Heuristic: NUL byte in the first 8 KiB means binary."""
    return b"\x00" in data[:8192]
