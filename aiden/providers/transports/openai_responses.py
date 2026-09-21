"""OpenAI Responses protocol (``openai-responses``).

Serves: ``openai`` (39 models) and 4 ``opencode-go`` models.

The Responses API is item-based rather than message-based: the request carries ``input``
items and the stream carries typed events (``response.output_text.delta``,
``response.function_call_arguments.delta``, ``response.output_item.added``, …).
We translate into the same IR as every other transport.
"""

from __future__ import annotations

import json
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
    ToolCallDelta,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
    ToolSpec,
    Usage,
)
from .base import HTTPTransport, RequestSpec, _StreamState

API_PATH = "/responses"


class OpenAIResponsesTransport(HTTPTransport):
    api = "openai-responses"

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
            "authorization": f"Bearer {credential_key}",
        }
        headers.update({str(k): str(v) for k, v in model.headers.items()})
        headers.update(provider_headers(model.provider, session_id))

        payload: dict[str, Any] = {
            "model": model.id,
            "input": _to_responses_input(messages),
            "stream": True,
        }
        if system:
            payload["instructions"] = system

        effective_max = max_tokens or model.max_tokens
        if effective_max:
            payload["max_output_tokens"] = effective_max

        compat = model.compat or {}
        if compat.get("supportsStore", True):
            payload["store"] = False

        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters or {"type": "object", "properties": {}},
                }
                for t in tools
            ]
            payload["tool_choice"] = "auto"

        if thinking_level and thinking_level != "off":
            effort = _map_effort(model, thinking_level)
            if effort:
                payload["reasoning"] = {"effort": effort}

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

        if kind == "response.output_text.delta":
            if data.get("delta"):
                out.append(TextDelta(text=data["delta"]))
        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            if data.get("delta"):
                out.append(ThinkingDelta(text=data["delta"]))
        elif kind == "response.output_item.added":
            item = data.get("item") or {}
            if item.get("type") == "function_call":
                state.pending_tool_ids[data.get("output_index", 0)] = (
                    item.get("call_id") or item.get("id") or ""
                )
                out.append(
                    ToolCallStart(
                        id=item.get("call_id") or item.get("id") or "",
                        name=item.get("name") or "",
                        index=int(data.get("output_index", 0)),
                    )
                )
        elif kind == "response.function_call_arguments.delta":
            index = int(data.get("output_index", 0))
            call_id = state.pending_tool_ids.get(index, data.get("item_id", ""))
            if data.get("delta"):
                out.append(ToolCallDelta(id=call_id, arguments_delta=data["delta"], index=index))
        elif kind == "response.completed":
            response = data.get("response") or {}
            usage = response.get("usage")
            if isinstance(usage, dict):
                state.usage = state.usage + _usage_from(usage)
            state.stop_reason = "tool_use" if state.tool_calls else "end_turn"
        elif kind in ("response.failed", "error"):
            error = data.get("error") or data.get("response", {}).get("error") or {}
            state.error = classify(
                None,
                json.dumps(error) if not isinstance(error, str) else error,
                provider=state.model.provider,
                model=state.model.id,
            )
            if isinstance(error, dict) and error.get("message"):
                state.error.message = error["message"]
            state.stop_reason = "error"
        elif kind == "response.incomplete":
            response = data.get("response") or {}
            details = response.get("incomplete_details") or {}
            state.stop_reason = (
                "max_tokens" if details.get("reason") == "max_output_tokens" else "end_turn"
            )

        return out


# --------------------------------------------------------------------------- conversion


def _to_responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            text = _join_text(message.content)
            if text:
                items.append({"role": "system", "content": text})
            continue

        if message.role == "tool":
            for part in message.content:
                if isinstance(part, ToolResultPart):
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": part.call_id,
                            "output": part.output or "(no output)",
                        }
                    )
            continue

        if message.role == "assistant":
            text = _join_text(message.content)
            if text:
                items.append({"role": "assistant", "content": text})
            for part in message.content:
                if isinstance(part, ToolCallPart):
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": part.id,
                            "name": part.name,
                            "arguments": part.raw_arguments or json.dumps(part.arguments),
                        }
                    )
            continue

        images = [p for p in message.content if isinstance(p, ImagePart)]
        if images:
            content: list[dict[str, Any]] = []
            text = _join_text(message.content)
            if text:
                content.append({"type": "input_text", "text": text})
            for image in images:
                content.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{image.media_type};base64,{image.data}",
                    }
                )
            items.append({"role": "user", "content": content})
        else:
            text = _join_text(message.content)
            if text:
                items.append({"role": "user", "content": text})

    return items


def _join_text(parts: list[Part]) -> str:
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            chunks.append(part.text)
        elif isinstance(part, ToolResultPart):
            chunks.append(part.output)
    return "\n".join(c for c in chunks if c)


def _usage_from(raw: dict[str, Any]) -> Usage:
    details = raw.get("input_tokens_details") or {}
    output_details = raw.get("output_tokens_details") or {}
    return Usage(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_read_tokens=int(details.get("cached_tokens") or 0),
        reasoning_tokens=int(output_details.get("reasoning_tokens") or 0),
    )


def _map_effort(model: ModelInfo, level: str) -> str | None:
    mapping = model.thinking_level_map or {}
    if level in mapping:
        return mapping[level]
    if level in ("xhigh", "max"):
        return "high"
    return level if level in ("minimal", "low", "medium", "high") else None
