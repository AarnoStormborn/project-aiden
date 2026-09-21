"""Transport registry: api name → implementation.

Adding a provider that speaks an already-implemented protocol requires **no code change**
(just a catalog entry). Adding a new protocol means one module plus one line here.
"""

from __future__ import annotations

import httpx

from ..errors import UnsupportedProtocolError
from ..types import ModelInfo
from .anthropic_messages import AnthropicMessagesTransport
from .base import HTTPTransport
from .openai_completions import OpenAICompletionsTransport
from .openai_responses import OpenAIResponsesTransport

TRANSPORTS: dict[str, type[HTTPTransport]] = {
    "anthropic-messages": AnthropicMessagesTransport,
    "openai-completions": OpenAICompletionsTransport,
    "openai-responses": OpenAIResponsesTransport,
    # "google-generative-ai": GoogleGenerativeAITransport,  # deferred (not requested)
}

_instances: dict[str, HTTPTransport] = {}


def transport_for(model: ModelInfo, *, client: httpx.AsyncClient | None = None) -> HTTPTransport:
    """Return a (cached) transport for ``model.api``."""
    try:
        factory = TRANSPORTS[model.api]
    except KeyError:
        raise UnsupportedProtocolError(model.api, provider=model.provider, model=model.id) from None
    key = model.api
    if client is not None:
        return factory(client)
    instance = _instances.get(key)
    if instance is None:
        instance = factory()
        _instances[key] = instance
    return instance


async def aclose_all() -> None:
    for instance in list(_instances.values()):
        await instance.aclose()
    _instances.clear()


def available_apis() -> list[str]:
    return sorted(TRANSPORTS)


__all__ = [
    "TRANSPORTS",
    "AnthropicMessagesTransport",
    "HTTPTransport",
    "OpenAICompletionsTransport",
    "OpenAIResponsesTransport",
    "aclose_all",
    "available_apis",
    "transport_for",
]
