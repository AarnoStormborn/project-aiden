"""Transport base: shared HTTP, retry of connection phase, and event plumbing.

Every protocol adapter subclasses :class:`HTTPTransport` and implements
:meth:`HTTPTransport.build_request` / :meth:`HTTPTransport.translate`.

Key contracts (docs/plan/providers.md §4):
- **Retry only the connection phase.** Once the first token has been emitted, a failure
  becomes a terminal ``Stop("error")`` — retrying mid-stream would duplicate content.
- **Errors are values.** ``stream()`` never raises for provider failures; it yields ``Stop``.
- ``max_tokens`` mid-tool-call is surfaced as ``Stop("max_tokens")`` so the loop drops the
  partial calls instead of executing truncated arguments.
"""

from __future__ import annotations

import abc
import asyncio
import json
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from ..capabilities import new_session_id, requires_session_id
from ..errors import (
    AuthError,
    ProviderError,
    RateLimitError,
    classify,
)
from ..sse import SSEEvent, iter_sse
from ..types import (
    Completion,
    Message,
    ModelInfo,
    Stop,
    StopReason,
    StreamEvent,
    ToolSpec,
    Usage,
)

DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)
MAX_CONNECT_ATTEMPTS = 3


@dataclass(slots=True)
class RequestSpec:
    url: str
    headers: dict[str, str]
    payload: dict[str, Any]


class HTTPTransport(abc.ABC):
    """Common behaviour for the HTTP-based protocols."""

    api: str = ""

    def __init__(
        self, client: httpx.AsyncClient | None = None, *, max_attempts: int = MAX_CONNECT_ATTEMPTS
    ):
        self._client = client
        self._owns_client = client is None
        self._client_loop: object | None = None
        self.max_attempts = max_attempts

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
            self._client_loop = None

    def client(self) -> httpx.AsyncClient:
        """Return an httpx client bound to the *current* event loop.

        ``transport_for()`` caches transports for the process lifetime while an
        ``httpx.AsyncClient`` is bound to the loop that created it. A CLI that calls
        ``asyncio.run()`` per invocation would otherwise reuse a client whose connection
        pool belongs to a closed loop. Recreate when the loop changes.
        """
        try:
            loop: object | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if self._client is None or (self._owns_client and self._client_loop is not loop):
            # The old client belongs to a dead loop; it cannot be closed from here, so
            # drop the reference and let GC reclaim it rather than awaiting on a dead loop.
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True)
            self._client_loop = loop
        return self._client

    # ------------------------------------------------------------------ protocol API

    @abc.abstractmethod
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
        """Produce the wire request for one assistant turn."""

    @abc.abstractmethod
    def translate(self, event: SSEEvent, state: _StreamState) -> list[StreamEvent]:
        """Map one SSE event to zero or more IR events."""

    def is_done(self, event: SSEEvent) -> bool:
        """Whether this SSE event terminates the stream (e.g. ``[DONE]``)."""
        return event.data.strip() == "[DONE]"

    def finalize(self, state: _StreamState) -> list[StreamEvent]:
        return []

    # ------------------------------------------------------------------ driving

    async def stream(
        self,
        *,
        model: ModelInfo,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        system: str = "",
        max_tokens: int | None = None,
        thinking_level: str | None = None,
        credential: Any = None,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        tools = tools or []
        key = getattr(credential, "key", credential) or ""
        ctype = getattr(credential, "type", "api_key") or "api_key"

        if session_id is None and requires_session_id(model.provider):
            session_id = new_session_id()

        spec = self.build_request(
            model=model,
            messages=messages,
            tools=tools,
            system=system,
            max_tokens=max_tokens,
            thinking_level=thinking_level,
            credential_key=str(key),
            credential_type=str(ctype),
            session_id=session_id,
        )

        state = _StreamState(model=model)
        yield state.start()

        response: httpx.Response | None = None
        try:
            response = await self._open_stream(spec, model)
        except ProviderError as exc:
            yield state.fail(exc)
            return

        try:
            async for event in iter_sse(response.aiter_text()):
                if self.is_done(event):
                    break
                for ir_event in self.translate(event, state):
                    state.observe(ir_event)
                    yield ir_event
        except (httpx.HTTPError, ProviderError) as exc:
            error = (
                exc
                if isinstance(exc, ProviderError)
                else classify(None, str(exc), provider=model.provider, model=model.id)
            )
            yield state.fail(error)
            return
        finally:
            # httpx responses are not async context managers unless obtained via
            # client.stream(); close explicitly so we never leak a connection.
            await response.aclose()

        for ir_event in self.finalize(state):
            yield ir_event
        yield state.stop_event()

    async def _open_stream(self, spec: RequestSpec, model: ModelInfo) -> httpx.Response:
        """Establish the streaming response, retrying only the connection phase."""
        last: ProviderError | None = None
        for attempt in range(self.max_attempts):
            try:
                request = self.client().build_request(
                    "POST", spec.url, headers=spec.headers, json=spec.payload
                )
                response = await self.client().send(request, stream=True)
            except httpx.HTTPError as exc:
                last = classify(None, str(exc), provider=model.provider, model=model.id)
                await self._sleep_backoff(attempt, None)
                continue

            if response.status_code < 400:
                return response

            body = (await response.aread()).decode("utf-8", "replace")
            retry_after = response.headers.get("retry-after")
            error = classify(
                response.status_code,
                body,
                provider=model.provider,
                model=model.id,
                retry_after_s=_parse_retry_after(retry_after),
            )
            await response.aclose()

            if isinstance(error, AuthError) or not error.retryable:
                raise error

            last = error
            await self._sleep_backoff(attempt, getattr(error, "retry_after_s", None))

        raise last or ProviderError(
            "connection failed", provider=model.provider, model=model.id, retryable=True
        )

    async def _sleep_backoff(self, attempt: int, retry_after_s: float | None) -> None:
        if attempt >= self.max_attempts - 1:
            return
        delay = retry_after_s if retry_after_s is not None else min(2**attempt, 8) + random.random()
        await asyncio.sleep(delay)


class _StreamState:
    """Mutable accumulation shared between ``translate`` calls and the final ``Stop``."""

    def __init__(self, model: ModelInfo):
        self.model = model
        self.usage = Usage()
        self.stop_reason: StopReason = "end_turn"
        self.error: ProviderError | None = None
        # index -> partial tool call state (authoritative accumulation)
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.text_parts: list[str] = []
        self.thinking_parts: list[str] = []
        self.thinking_signature: str = ""
        self.model_name: str = model.id
        # protocol-specific scratch (e.g. Responses output_index → call_id)
        self.pending_tool_ids: dict[int, str] = {}

    # -- accumulation -------------------------------------------------------

    def observe(self, event: StreamEvent) -> None:
        """Fold a translated event into the accumulated completion.

        Transports translate wire events to IR; accumulation happens here once, so a new
        adapter cannot forget to record text or tool arguments.
        """
        from ..types import TextDelta, ThinkingDelta, ToolCallDelta, ToolCallStart

        if isinstance(event, TextDelta):
            self.text_parts.append(event.text)
        elif isinstance(event, ThinkingDelta):
            self.thinking_parts.append(event.text)
        elif isinstance(event, ToolCallStart):
            self.tool_calls[event.index] = {
                "id": event.id,
                "name": event.name,
                "arguments": "",
            }
        elif isinstance(event, ToolCallDelta):
            entry = self.tool_calls.setdefault(
                event.index, {"id": event.id, "name": "", "arguments": ""}
            )
            if event.id and not entry.get("id"):
                entry["id"] = event.id
            entry["arguments"] = entry.get("arguments", "") + event.arguments_delta

    # -- emission -----------------------------------------------------------

    def start(self) -> StreamEvent:
        from ..types import Start

        return Start(model=self.model.ref)

    def fail(self, error: ProviderError) -> Stop:
        self.error = error
        return Stop(
            reason="error",
            usage=self.usage,
            cost_usd=self.model.cost.of(self.usage),
            message=str(error),
        )

    def stop_event(self) -> Stop:
        return Stop(
            reason=self.stop_reason,
            usage=self.usage,
            cost_usd=self.model.cost.of(self.usage),
            message=self.diagnostic(),
        )

    def diagnostic(self) -> str:
        """Explain a suspicious stop, so a cap never silently eats output.

        research/02 §3: every cap must leave a trace. The two real cases we have seen are
        reasoning consuming the entire budget (empty text, non-zero reasoning tokens) and a
        tool call truncated mid-arguments.
        """
        if self.error is not None:
            return self.error.message
        if self.stop_reason != "max_tokens":
            return ""
        if not self.text_parts and not self.tool_calls and self.usage.reasoning_tokens:
            return (
                f"output budget exhausted by reasoning ({self.usage.reasoning_tokens} reasoning "
                f"tokens, no visible text); raise max_tokens or lower the thinking level"
            )
        if self.tool_calls:
            return "output truncated by max_tokens; tool call arguments may be partial"
        return "output truncated by max_tokens"

    def completion(self) -> Completion:
        from ..types import ToolCallPart

        calls: list[ToolCallPart] = []
        for index in sorted(self.tool_calls):
            raw = self.tool_calls[index]
            arguments: dict[str, Any] = {}
            text = raw.get("arguments", "")
            truncated = False
            if text.strip():
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        arguments = parsed
                    else:
                        truncated = True
                except json.JSONDecodeError:
                    truncated = True
            elif self.stop_reason == "max_tokens":
                truncated = True
            calls.append(
                ToolCallPart(
                    id=raw.get("id", ""),
                    name=raw.get("name", ""),
                    arguments=arguments,
                    raw_arguments=text,
                    truncated=truncated,
                )
            )
        return Completion(
            text="".join(self.text_parts),
            thinking="".join(self.thinking_parts),
            thinking_signature=self.thinking_signature,
            tool_calls=calls,
            usage=self.usage,
            cost_usd=self.model.cost.of(self.usage),
            stop_reason=self.stop_reason,
            error=self.error.message if self.error else "",
            diagnostic=self.diagnostic(),
        )


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - __import__("time").time())
        except Exception:
            return None


def bearer_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


__all__ = [
    "HTTPTransport",
    "RateLimitError",
    "RequestSpec",
    "bearer_headers",
]
