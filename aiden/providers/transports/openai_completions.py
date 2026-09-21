"""OpenAI Chat Completions protocol (``openai-completions``).

Serves: ``opencode-go`` (21 models), ``deepseek``, ``commandcode`` (non-Claude models),
and effectively any OpenAI-compatible endpoint.

Compat flags honoured from the catalog (``model.compat``):
- ``supportsDeveloperRole`` (default true) — use a ``developer`` message for the system
  prompt; some servers only understand ``system``.
- ``maxTokensField`` (default ``max_completion_tokens``) — deepseek/opencode-go need
  ``max_tokens``.
- ``supportsStore`` (default true) — omit ``store`` when false.
- ``requiresReasoningContentOnAssistantMessages`` — replay ``reasoning_content`` on
  assistant turns, or the provider rejects the history.
- ``thinkingFormat`` — ``"deepseek"`` style reasoning vs OpenAI ``reasoning_effort``.
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
    ThinkingPart,
    ToolCallDelta,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
    ToolSpec,
    Usage,
)
from .base import HTTPTransport, RequestSpec, _StreamState

API_PATH = "/chat/completions"

_FINISH_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "end_turn",
}

REASONING_EFFORT_LEVELS = {"minimal", "low", "medium", "high"}


class OpenAICompletionsTransport(HTTPTransport):
    api = "openai-completions"

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
        compat = model.compat or {}
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "authorization": f"Bearer {credential_key}",
        }
        headers.update({str(k): str(v) for k, v in model.headers.items()})
        headers.update(provider_headers(model.provider, session_id))

        body_messages = _to_openai_messages(
            messages,
            system=system,
            developer_role=bool(compat.get("supportsDeveloperRole", True)),
            replay_reasoning=bool(compat.get("requiresReasoningContentOnAssistantMessages", False)),
            supports_mid_convo_system=bool(compat.get("supportsMidConvoSystemMessages", False)),
        )

        payload: dict[str, Any] = {
            "model": model.id,
            "messages": body_messages,
            "stream": True,
        }

        max_field = compat.get("maxTokensField") or "max_completion_tokens"
        effective_max = max_tokens or model.max_tokens
        if effective_max:
            payload[max_field] = effective_max

        if compat.get("supportsStore", True):
            payload["store"] = False

        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters or {"type": "object", "properties": {}},
                    },
                }
                for t in tools
            ]
            payload["tool_choice"] = "auto"

        if thinking_level and thinking_level != "off":
            mapped = _map_thinking(model, thinking_level)
            if mapped:
                payload["reasoning_effort"] = mapped

        return RequestSpec(url=f"{model.base_url}{API_PATH}", headers=headers, payload=payload)

    # ------------------------------------------------------------------ translate

    def translate(self, event: SSEEvent, state: _StreamState) -> list[StreamEvent]:
        if not event.data or event.data.strip() == "[DONE]":
            return []
        try:
            data = event.json()
        except Exception:
            return []

        if isinstance(data.get("error"), dict) and data["error"]:
            error = data["error"]
            state.error = classify(
                None,
                json.dumps(error),
                provider=state.model.provider,
                model=state.model.id,
            )
            state.error.message = error.get("message") or state.error.message
            state.stop_reason = "error"
            return []

        usage = data.get("usage")
        if isinstance(usage, dict):
            state.usage = state.usage + _usage_from(usage)

        out: list[StreamEvent] = []
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}

            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                out.append(ThinkingDelta(text=reasoning))

            content = delta.get("content")
            if isinstance(content, str) and content:
                out.append(TextDelta(text=content))
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        out.append(TextDelta(text=block.get("text", "")))

            for call in delta.get("tool_calls") or []:
                index = int(call.get("index", 0))
                function = call.get("function") or {}
                call_id = call.get("id") or ""
                if function.get("name"):
                    out.append(ToolCallStart(id=call_id, name=function["name"], index=index))
                fragment = function.get("arguments")
                if fragment:
                    out.append(ToolCallDelta(id=call_id, arguments_delta=fragment, index=index))

            reason = _FINISH_REASONS.get(choice.get("finish_reason") or "")
            if reason:
                state.stop_reason = reason  # type: ignore[assignment]

        return out

    def is_done(self, event: SSEEvent) -> bool:
        return event.data.strip() == "[DONE]"


# --------------------------------------------------------------------------- conversion


def _to_openai_messages(
    messages: list[Message],
    *,
    system: str,
    developer_role: bool,
    replay_reasoning: bool,
    supports_mid_convo_system: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "developer" if developer_role else "system", "content": system})

    for message in messages:
        if message.role == "system":
            text = _join_text(message.content)
            if not text:
                continue
            if supports_mid_convo_system:
                out.append({"role": "system", "content": text})
            elif out and out[0]["role"] in ("system", "developer"):
                out[0]["content"] = f"{out[0]['content']}\n\n{text}"
            else:
                out.append({"role": "system", "content": text})
            continue

        if message.role == "tool":
            for part in message.content:
                if isinstance(part, ToolResultPart):
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": part.call_id,
                            "content": part.output or "(no output)",
                        }
                    )
            continue

        if message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant"}
            text = _join_text(message.content)
            if text:
                entry["content"] = text
            calls = [p for p in message.content if isinstance(p, ToolCallPart)]
            if calls:
                entry["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": c.raw_arguments or json.dumps(c.arguments),
                        },
                    }
                    for c in calls
                ]
            if not text and not calls:
                continue
            if replay_reasoning:
                thinking = _join_thinking(message.content)
                if thinking:
                    entry["reasoning_content"] = thinking
            out.append(entry)
            continue

        # user
        images = [p for p in message.content if isinstance(p, ImagePart)]
        if images:
            content: list[dict[str, Any]] = []
            text = _join_text(message.content)
            if text:
                content.append({"type": "text", "text": text})
            for image in images:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image.media_type};base64,{image.data}"},
                    }
                )
            out.append({"role": "user", "content": content})
        else:
            text = _join_text(message.content)
            if text:
                out.append({"role": "user", "content": text})

    return out


def _join_text(parts: list[Part]) -> str:
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            chunks.append(part.text)
        elif isinstance(part, ToolResultPart):
            chunks.append(part.output)
    return "\n".join(c for c in chunks if c)


def _join_thinking(parts: list[Part]) -> str:
    return "\n".join(p.text for p in parts if isinstance(p, ThinkingPart) and p.text)


def _usage_from(raw: dict[str, Any]) -> Usage:
    details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    return Usage(
        input_tokens=int(raw.get("prompt_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cache_read_tokens=int(details.get("cached_tokens") or 0),
        reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
    )


def _map_thinking(model: ModelInfo, level: str) -> str | None:
    """Prefer the model's declared level map; fall back to OpenAI effort names."""
    mapping = model.thinking_level_map or {}
    if level in mapping:
        return mapping[level]
    if level == "xhigh" or level == "max":
        return "high" if "high" in REASONING_EFFORT_LEVELS else None
    return level if level in REASONING_EFFORT_LEVELS else None
