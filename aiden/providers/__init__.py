"""Aiden's provider suite.

One IR (``types``), one catalog (``catalog``), one auth resolver (``auth``), and one
adapter per wire protocol (``transports``).

Typical use::

    from aiden.providers import ProviderSuite

    suite = ProviderSuite.load()
    model = suite.catalog.resolve("opencode-go/muse-spark-1.3-contributor")
    async for event in suite.stream(model, messages, tools, system="…"):
        ...

See docs/plan/providers.md for the design and the build order.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .auth import Credential, import_pi, require, resolve, status
from .catalog import Catalog
from .errors import (
    AuthError,
    BadRequestError,
    OverflowError_,
    ProviderError,
    RateLimitError,
    TransportError,
    UnsupportedProtocolError,
)
from .registry import ProviderRegistry
from .transports import aclose_all, available_apis, transport_for
from .types import (
    Completion,
    Cost,
    ImagePart,
    Message,
    ModelInfo,
    ProviderInfo,
    Start,
    Stop,
    StopReason,
    StreamEvent,
    TextDelta,
    TextPart,
    ThinkingDelta,
    ThinkingPart,
    ToolCallDelta,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
    ToolSpec,
    Usage,
)

__all__ = [
    "AuthError",
    "BadRequestError",
    "Catalog",
    "Completion",
    "Cost",
    "Credential",
    "ImagePart",
    "Message",
    "ModelInfo",
    "OverflowError_",
    "ProviderError",
    "ProviderInfo",
    "ProviderRegistry",
    "ProviderSuite",
    "RateLimitError",
    "Start",
    "Stop",
    "StopReason",
    "StreamEvent",
    "TextDelta",
    "TextPart",
    "ThinkingDelta",
    "ThinkingPart",
    "ToolCallDelta",
    "ToolCallPart",
    "ToolCallStart",
    "ToolResultPart",
    "ToolSpec",
    "TransportError",
    "UnsupportedProtocolError",
    "Usage",
    "aclose_all",
    "available_apis",
    "import_pi",
    "require",
    "resolve",
    "status",
    "transport_for",
]


class ProviderSuite:
    """Convenience façade: catalog + auth + transports in one object."""

    def __init__(self, catalog: Catalog | None = None, *, api_key: str | None = None):
        self.catalog = catalog or Catalog.load()
        self.registry = ProviderRegistry(self.catalog)
        self._api_key = api_key

    @classmethod
    def load(cls, **kw: Any) -> ProviderSuite:
        return cls(**kw)

    # ------------------------------------------------------------------ discovery

    def resolve(self, ref: str) -> ModelInfo:
        return self.catalog.resolve(ref)

    def search(self, pattern: str) -> list[ModelInfo]:
        return self.catalog.search(pattern)

    def credential(self, provider: str) -> Credential | None:
        return resolve(provider, api_key=self._api_key)

    def authenticated_providers(self) -> list[str]:
        return [p.id for p in self.catalog.providers_with_models() if resolve(p.id) is not None]

    # ------------------------------------------------------------------ streaming

    async def stream(
        self,
        model: ModelInfo | str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        system: str = "",
        max_tokens: int | None = None,
        thinking_level: str | None = None,
        credential: Credential | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        info = self.resolve(model) if isinstance(model, str) else model
        cred = credential or require(info.provider, api_key=self._api_key)
        transport = transport_for(info)
        async for event in transport.stream(
            model=info,
            messages=messages,
            tools=tools or [],
            system=system,
            max_tokens=max_tokens,
            thinking_level=thinking_level,
            credential=cred,
            session_id=session_id,
        ):
            yield event

    async def complete(
        self,
        model: ModelInfo | str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        **kw: Any,
    ) -> Completion:
        """Run one turn and return the accumulated completion."""
        import json

        completion = Completion()
        calls: dict[int, dict[str, str]] = {}

        async for event in self.stream(model, messages, tools, **kw):
            if isinstance(event, TextDelta):
                completion.text += event.text
            elif isinstance(event, ThinkingDelta):
                completion.thinking += event.text
            elif isinstance(event, ToolCallStart):
                calls[event.index] = {"id": event.id, "name": event.name, "arguments": ""}
            elif isinstance(event, ToolCallDelta):
                entry = calls.setdefault(event.index, {"id": event.id, "name": "", "arguments": ""})
                if event.id and not entry["id"]:
                    entry["id"] = event.id
                entry["arguments"] += event.arguments_delta
            elif isinstance(event, Stop):
                completion.usage = event.usage
                completion.cost_usd = event.cost_usd
                completion.stop_reason = event.reason
                completion.error = event.message

        for index in sorted(calls):
            raw = calls[index]
            arguments: dict[str, Any] = {}
            if raw["arguments"].strip():
                try:
                    parsed = json.loads(raw["arguments"])
                    if isinstance(parsed, dict):
                        arguments = parsed
                except json.JSONDecodeError:
                    arguments = {}
            completion.tool_calls.append(
                ToolCallPart(
                    id=raw["id"],
                    name=raw["name"],
                    arguments=arguments,
                    raw_arguments=raw["arguments"],
                )
            )

        return completion
