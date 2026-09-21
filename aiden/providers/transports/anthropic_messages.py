"""Anthropic Messages protocol (``anthropic-messages``).

Serves: ``anthropic`` directly, plus ``opencode-go`` and ``commandcode`` Claude models
through their compatible gateways.

Auth: ``x-api-key`` by default; ``Authorization: Bearer`` when the credential is an
Anthropic OAuth token (``sk-ant-oat…``), matching pi's behaviour
(``api/anthropic-messages.js`` createClient/isOAuthToken).
"""

from __future__ import annotations

from typing import Any

from ..capabilities import provider_headers
from ..errors import classify
from ..sse import SSEEvent
from ..types import (
    ImagePart,
    Message,
    ModelInfo,
    Part,
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
from .base import HTTPTransport, RequestSpec, _StreamState

ANTHROPIC_VERSION = "2023-06-01"
API_PATH = "/v1/messages"

# Thinking-level → token budget, mirroring pi's ladder (clamped below max_tokens).
THINKING_BUDGETS: dict[str, int] = {
    "minimal": 1_024,
    "low": 2_048,
    "medium": 4_096,
    "high": 8_192,
    "xhigh": 16_384,
    "max": 32_768,
}

_STOP_REASONS = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "end_turn",
    "pause_turn": "end_turn",
}


class AnthropicMessagesTransport(HTTPTransport):
    api = "anthropic-messages"

    # ------------------------------------------------------------------ request

    def build_request(
        self,
        *,
        model: ModelInfo,
        messages: list[Message],
        tools: list[ToolSpec],
        system: str,
        max_tokens: int | None,
        thinking_level: str | None,
        credential_key: str,
        credential_type: str,
        session_id: str | None = None,
    ) -> RequestSpec:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if credential_type == "oauth" or credential_key.startswith("sk-ant-oat"):
            headers["authorization"] = f"Bearer {credential_key}"
        else:
            headers["x-api-key"] = credential_key
        headers.update(_stringify(model.headers))
        headers.update(provider_headers(model.provider, session_id))

        effective_max = max_tokens or model.max_tokens or 8_192
        payload: dict[str, Any] = {
            "model": model.id,
            "max_tokens": effective_max,
            "messages": _to_anthropic_messages(messages),
            "stream": True,
        }

        if system:
            payload["system"] = system

        if tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters or {"type": "object", "properties": {}},
                }
                for t in tools
            ]

        budget = THINKING_BUDGETS.get(thinking_level or "")
        if budget:
            # Anthropic requires budget_tokens < max_tokens; reserve 1024 like pi.
            budget = min(budget, max(0, effective_max - 1_024))
            if budget >= 1_024:
                payload["thinking"] = {"type": "enabled", "budget_tokens": budget}

        return RequestSpec(url=f"{model.base_url}{API_PATH}", headers=headers, payload=payload)

    # ------------------------------------------------------------------ translate

    def translate(self, event: SSEEvent, state: _StreamState) -> list[StreamEvent]:
        if not event.data:
            return []
        try:
            data = event.json()
        except Exception:
            return []

        kind = data.get("type") or event.event
        out: list[StreamEvent] = []

        if kind == "message_start":
            message = data.get("message") or {}
            state.usage = _usage_from(message.get("usage") or {})
            model_id = message.get("model")
            if model_id:
                state.model_name = model_id
        elif kind == "content_block_start":
            block = data.get("content_block") or {}
            index = int(data.get("index", 0))
            if block.get("type") == "tool_use":
                # Record id/name for later delta correlation; observe() owns accumulation.
                state.tool_calls.setdefault(
                    index,
                    {"id": block.get("id", ""), "name": block.get("name", ""), "arguments": ""},
                )
                out.append(
                    ToolCallStart(id=block.get("id", ""), name=block.get("name", ""), index=index)
                )
        elif kind == "content_block_delta":
            delta = data.get("delta") or {}
            index = int(data.get("index", 0))
            dtype = delta.get("type")
            if dtype == "text_delta" and delta.get("text"):
                out.append(TextDelta(text=delta["text"]))
            elif dtype == "thinking_delta" and delta.get("thinking"):
                out.append(ThinkingDelta(text=delta["thinking"]))
            elif dtype == "signature_delta" and delta.get("signature"):
                state.thinking_signature += delta["signature"]
            elif dtype == "input_json_delta":
                fragment = delta.get("partial_json") or ""
                entry = state.tool_calls.get(index) or {"id": "", "name": "", "arguments": ""}
                # Accumulation is owned by _StreamState.observe; only the id is needed here.
                state.tool_calls.setdefault(index, entry)
                out.append(
                    ToolCallDelta(id=entry.get("id", ""), arguments_delta=fragment, index=index)
                )
        elif kind == "message_delta":
            delta = data.get("delta") or {}
            reason = _STOP_REASONS.get(delta.get("stop_reason") or "")
            if reason:
                state.stop_reason = reason  # type: ignore[assignment]
            usage = data.get("usage") or {}
            if usage:
                merged = _usage_from(usage)
                # message_delta carries cumulative output tokens.
                state.usage.output_tokens = merged.output_tokens or state.usage.output_tokens
                state.usage.cache_read_tokens = max(
                    state.usage.cache_read_tokens, merged.cache_read_tokens
                )
                state.usage.cache_write_tokens = max(
                    state.usage.cache_write_tokens, merged.cache_write_tokens
                )
        elif kind == "error":
            error = data.get("error") or {}
            state.error = classify(
                None,
                str(error),
                provider=state.model.provider,
                model=state.model.id,
            )
            state.error.message = error.get("message") or state.error.message
            state.stop_reason = "error"
        return out


# --------------------------------------------------------------------------- conversion


def _to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        role = "assistant" if message.role == "assistant" else "user"
        blocks: list[dict[str, Any]] = []
        for part in message.content:
            block = _to_anthropic_block(part)
            if block is not None:
                blocks.append(block)
        if not blocks:
            continue
        if out and out[-1]["role"] == role and role == "user" and _only_tool_results(blocks):
            # Merge consecutive tool_result carriers so Anthropic sees one user turn.
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    return out


def _only_tool_results(blocks: list[dict[str, Any]]) -> bool:
    return bool(blocks) and all(b.get("type") == "tool_result" for b in blocks)


def _to_anthropic_block(part: Part) -> dict[str, Any] | None:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text} if part.text else None
    if isinstance(part, ImagePart):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": part.media_type,
                "data": part.data,
            },
        }
    if isinstance(part, ThinkingPart):
        if part.redacted:
            return {"type": "redacted_thinking", "data": part.text}
        block = {"type": "thinking", "thinking": part.text}
        if part.signature:
            block["signature"] = part.signature
        return block
    if isinstance(part, ToolCallPart):
        return {
            "type": "tool_use",
            "id": part.id,
            "name": part.name,
            "input": part.arguments,
        }
    if isinstance(part, ToolResultPart):
        return {
            "type": "tool_result",
            "tool_use_id": part.call_id,
            "content": part.output,
            "is_error": part.is_error,
        }
    # ``Part`` is a closed union, so mypy sees this as unreachable. It is kept as an
    # explicit None so an unknown part type degrades to "drop it" rather than raising
    # mid-stream, and so adding a Part variant fails loudly in tests, not in production.
    return None  # type: ignore[unreachable]


def _usage_from(raw: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_read_tokens=int(raw.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(raw.get("cache_creation_input_tokens") or 0),
    )


def _stringify(headers: dict[str, Any]) -> dict[str, str]:
    return {str(k): str(v) for k, v in headers.items()}
