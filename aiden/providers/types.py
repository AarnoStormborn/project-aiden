"""Unified provider IR.

Every transport converts to and from these types, so the loop, the session log and
the tool layer never see a provider-specific shape.

Design notes (see docs/plan/providers.md §4):
- ``StreamEvent`` is the only thing a transport yields. Failures are events, not raises.
- ``Usage`` carries cache read/write and reasoning tokens separately, because the
  architecture requires per-entry cost attribution including summarization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "error", "aborted"]
Api = Literal[
    "anthropic-messages",
    "openai-completions",
    "openai-responses",
    "google-generative-ai",
]


# --------------------------------------------------------------------------- content


@dataclass(slots=True)
class TextPart:
    text: str
    type: Literal["text"] = "text"


@dataclass(slots=True)
class ImagePart:
    """Base64 image payload, provider-agnostic."""

    media_type: str
    data: str
    type: Literal["image"] = "image"


@dataclass(slots=True)
class ThinkingPart:
    """Reasoning block. ``signature`` is Anthropic's replay token; opaque elsewhere."""

    text: str
    signature: str = ""
    redacted: bool = False
    type: Literal["thinking"] = "thinking"


@dataclass(slots=True)
class ToolCallPart:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Raw JSON as streamed, kept so a truncated call is detectable and replayable.
    raw_arguments: str = ""
    # True when the arguments could not be parsed or the turn hit max_tokens: the call
    # must NOT be executed (research/01 §loop: fail truncated calls, don't run them).
    truncated: bool = False
    type: Literal["tool_call"] = "tool_call"


@dataclass(slots=True)
class ToolResultPart:
    call_id: str
    output: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


Part = TextPart | ImagePart | ThinkingPart | ToolCallPart | ToolResultPart


@dataclass(slots=True)
class Message:
    role: Role
    content: list[Part] = field(default_factory=list)

    @classmethod
    def text(cls, role: Role, text: str) -> Message:
        return cls(role=role, content=[TextPart(text=text)])

    def parts(self, kind: type | tuple[type, ...]) -> list[Part]:
        if isinstance(kind, tuple):
            return [p for p in self.content if isinstance(p, kind)]
        return [p for p in self.content if isinstance(p, kind)]

    @property
    def tool_calls(self) -> list[ToolCallPart]:
        return [p for p in self.content if isinstance(p, ToolCallPart)]


# --------------------------------------------------------------------------- tools


@dataclass(slots=True)
class ToolSpec:
    """A tool exposed to the model. ``parameters`` is JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def as_json_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters or {"type": "object", "properties": {}},
        }


# --------------------------------------------------------------------------- usage


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


@dataclass(slots=True)
class Cost:
    """USD per million tokens, matching the catalog's ``cost`` block."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0

    def of(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_read_tokens * self.cache_read
            + usage.cache_write_tokens * self.cache_write
        ) / 1_000_000

    @classmethod
    def from_catalog(cls, raw: dict[str, Any] | None) -> Cost:
        raw = raw or {}
        return cls(
            input=float(raw.get("input", 0)),
            output=float(raw.get("output", 0)),
            cache_read=float(raw.get("cacheRead", 0)),
            cache_write=float(raw.get("cacheWrite", 0)),
        )


# --------------------------------------------------------------------------- catalog


@dataclass(slots=True)
class ModelInfo:
    id: str
    provider: str
    api: str
    base_url: str
    name: str = ""
    reasoning: bool = False
    input_modalities: tuple[str, ...] = ("text",)
    context_window: int = 0
    max_tokens: int = 0
    cost: Cost = field(default_factory=Cost)
    compat: dict[str, Any] = field(default_factory=dict)
    thinking_level_map: dict[str, str | None] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.provider}/{self.id}"

    def supports_images(self) -> bool:
        return "image" in self.input_modalities


@dataclass(slots=True)
class ProviderInfo:
    id: str
    models: dict[str, ModelInfo] = field(default_factory=dict)
    label: str = ""

    @property
    def apis(self) -> set[str]:
        return {m.api for m in self.models.values()}


# --------------------------------------------------------------------------- streaming


@dataclass(slots=True)
class Start:
    model: str
    type: Literal["start"] = "start"


@dataclass(slots=True)
class TextDelta:
    text: str
    type: Literal["text_delta"] = "text_delta"


@dataclass(slots=True)
class ThinkingDelta:
    text: str
    type: Literal["thinking_delta"] = "thinking_delta"


@dataclass(slots=True)
class ToolCallStart:
    id: str
    name: str
    index: int = 0
    type: Literal["tool_call_start"] = "tool_call_start"


@dataclass(slots=True)
class ToolCallDelta:
    id: str
    arguments_delta: str
    index: int = 0
    type: Literal["tool_call_delta"] = "tool_call_delta"


@dataclass(slots=True)
class UsageEvent:
    usage: Usage
    type: Literal["usage"] = "usage"


@dataclass(slots=True)
class Stop:
    reason: StopReason = "end_turn"
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    message: str = ""
    type: Literal["stop"] = "stop"


StreamEvent = Start | TextDelta | ThinkingDelta | ToolCallStart | ToolCallDelta | UsageEvent | Stop


@dataclass(slots=True)
class Completion:
    """Accumulated result of one streamed assistant turn."""

    text: str = ""
    thinking: str = ""
    thinking_signature: str = ""
    tool_calls: list[ToolCallPart] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    stop_reason: StopReason = "end_turn"
    error: str = ""
    # Human-readable explanation of a suspicious stop (truncation, exhausted budget).
    diagnostic: str = ""

    def to_message(self) -> Message:
        parts: list[Part] = []
        if self.thinking:
            parts.append(ThinkingPart(text=self.thinking, signature=self.thinking_signature))
        if self.text:
            parts.append(TextPart(text=self.text))
        parts.extend(self.tool_calls)
        return Message(role="assistant", content=parts)
