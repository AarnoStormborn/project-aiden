"""Tool registry and dispatch (L4).

Seven tools is the target surface (research/02 §2); v0.1 ships the three read-only ones.
Dispatch is the single place where a tool call becomes a result, so the guards live here:

  schema -> tool lookup -> run -> (never raises) -> ToolResult

A call for an unknown tool is an **error result**, not an exception: the model can see it and
correct itself ([r01] §validation).
"""

from __future__ import annotations

from typing import Any

from ..providers.types import ToolSpec
from .bash import BashTool
from .command_policy import CommandPolicy
from .edit import EditTool
from .glob import GlobTool
from .grep import GrepTool
from .net_policy import FetchPolicy
from .policy import WritePolicy
from .read import ReadTool
from .types import ReadState, Tool, ToolContext, ToolResult, preview_of
from .webfetch import WebFetchTool
from .write import WriteTool

#: Registered tools by name. Adding one is a single entry here plus a `spec()`.
_IMPLEMENTATIONS: tuple[Tool, ...] = (
    ReadTool(),
    GrepTool(),
    GlobTool(),
    EditTool(),
    WriteTool(),
    BashTool(),
    WebFetchTool(),
)

TOOLS: dict[str, Tool] = {tool.name: tool for tool in _IMPLEMENTATIONS}

#: Tools that can change the working tree. These take a checkpoint before running.
MUTATING_TOOLS = frozenset(name for name, tool in TOOLS.items() if getattr(tool, "mutating", False))

#: Tools that go through the approval gate. Wider than the mutating set on purpose: a fetch changes
#: nothing locally and still sends a request outward, which the user should see. Keying the gate on
#: "mutating" meant `web_fetch` would have run with no gate at all.
GATED_TOOLS = frozenset(
    name
    for name, tool in TOOLS.items()
    if getattr(tool, "mutating", False) or hasattr(tool, "approval_required")
)


def tool_specs() -> list[ToolSpec]:
    """The schemas sent to the provider."""
    return [tool.spec() for tool in TOOLS.values()]


def execute(name: str, arguments: Any, ctx: ToolContext) -> ToolResult:
    """Run a tool by name. Never raises for tool-level failures.

    ``arguments`` is typed ``Any`` on purpose: it comes from provider JSON and is therefore
    untrusted, so the object check below is a real guard rather than dead code.
    """
    tool = TOOLS.get(name)
    if tool is None:
        known = ", ".join(sorted(TOOLS))
        return ToolResult.error(f"unknown tool '{name}'. Available tools: {known}.")

    if not isinstance(arguments, dict):
        return ToolResult.error(
            f"arguments for '{name}' must be a JSON object, got {type(arguments).__name__}."
        )

    try:
        return tool.run(arguments, ctx)
    except Exception as exc:
        # A tool bug is reported to the model as an error result so the run continues and the
        # transcript records what happened. Swallowing it silently would be worse.
        return ToolResult.error(f"tool '{name}' failed unexpectedly: {type(exc).__name__}: {exc}")


__all__ = [
    "GATED_TOOLS",
    "MUTATING_TOOLS",
    "TOOLS",
    "BashTool",
    "CommandPolicy",
    "EditTool",
    "FetchPolicy",
    "GlobTool",
    "GrepTool",
    "ReadState",
    "ReadTool",
    "Tool",
    "ToolContext",
    "ToolResult",
    "WebFetchTool",
    "WritePolicy",
    "WriteTool",
    "execute",
    "preview_of",
    "tool_specs",
]
