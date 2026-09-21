"""Fixture-driven transport tests: no network, real recorded-stream shapes."""

from __future__ import annotations

import json

import pytest

from aiden.providers.sse import iter_sse_lines
from aiden.providers.transports import (
    AnthropicMessagesTransport,
    OpenAICompletionsTransport,
    OpenAIResponsesTransport,
)
from aiden.providers.transports.base import _StreamState
from aiden.providers.types import (
    ImagePart,
    Message,
    ModelInfo,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    Usage,
)

from ..conftest import FakeResponse, sse

# --------------------------------------------------------------------------- models


def anthropic_model() -> ModelInfo:
    return ModelInfo(
        id="claude-sonnet-4-6",
        provider="anthropic",
        api="anthropic-messages",
        base_url="https://api.anthropic.com",
        max_tokens=64_000,
        context_window=1_000_000,
        reasoning=True,
    )


def deepseek_model() -> ModelInfo:
    return ModelInfo(
        id="deepseek-flash",
        provider="deepseek",
        api="openai-completions",
        base_url="https://api.deepseek.com",
        max_tokens=384_000,
        reasoning=True,
        cost=json
        and __import__("aiden.providers.types", fromlist=["Cost"]).Cost(
            input=0.3, output=1.2, cache_read=0.006
        ),
        compat={
            "supportsStore": False,
            "supportsDeveloperRole": False,
            "maxTokensField": "max_tokens",
            "requiresReasoningContentOnAssistantMessages": True,
        },
        thinking_level_map={"low": "low", "high": "high", "max": "max"},
    )


def openai_model() -> ModelInfo:
    return ModelInfo(
        id="gpt-5.5",
        provider="openai",
        api="openai-responses",
        base_url="https://api.openai.com/v1",
        max_tokens=128_000,
    )


def collect(transport, state, lines):
    """Mirror the driver loop: translate, then fold into state (as stream() does)."""
    out = []
    for event in iter_sse_lines(lines):
        if transport.is_done(event):
            break
        for ir_event in transport.translate(event, state):
            state.observe(ir_event)
            out.append(ir_event)
    return out


# --------------------------------------------------------------------------- anthropic


def test_anthropic_request_shape_and_auth_header():
    transport = AnthropicMessagesTransport()
    spec = transport.build_request(
        model=anthropic_model(),
        messages=[Message.text("user", "hi")],
        tools=[ToolSpec(name="read", description="read a file", parameters={"type": "object"})],
        system="be terse",
        max_tokens=4096,
        thinking_level="high",
        credential_key="sk-ant-api-key",
        credential_type="api_key",
    )
    assert spec.url == "https://api.anthropic.com/v1/messages"
    assert spec.headers["x-api-key"] == "sk-ant-api-key"
    assert "authorization" not in spec.headers
    assert spec.headers["anthropic-version"] == "2023-06-01"
    assert spec.payload["system"] == "be terse"
    assert spec.payload["stream"] is True
    assert spec.payload["max_tokens"] == 4096
    assert spec.payload["tools"][0]["input_schema"] == {"type": "object"}
    assert (
        spec.payload["thinking"]["budget_tokens"] == 3072
    )  # high(8192) clamped to max_tokens-1024


def test_anthropic_oauth_token_switches_to_bearer():
    transport = AnthropicMessagesTransport()
    spec = transport.build_request(
        model=anthropic_model(),
        messages=[],
        tools=[],
        system="",
        max_tokens=None,
        thinking_level=None,
        credential_key="sk-ant-oat01-xyz",
        credential_type="oauth",
    )
    assert spec.headers["authorization"] == "Bearer sk-ant-oat01-xyz"
    assert "x-api-key" not in spec.headers


def test_anthropic_proxy_base_url_gets_v1_messages():
    model = anthropic_model()
    model.base_url = "https://api.commandcode.ai/provider"
    spec = AnthropicMessagesTransport().build_request(
        model=model,
        messages=[],
        tools=[],
        system="",
        max_tokens=None,
        thinking_level=None,
        credential_key="k",
        credential_type="api_key",
    )
    assert spec.url == "https://api.commandcode.ai/provider/v1/messages"


def test_anthropic_message_conversion_tool_flow():
    transport = AnthropicMessagesTransport()
    messages = [
        Message.text("user", "read the brief"),
        Message(
            role="assistant",
            content=[
                ToolCallPart(id="t1", name="read", arguments={"path": "docs/BRIEF.md"}),
            ],
        ),
        Message(role="tool", content=[ToolResultPart(call_id="t1", output="# Brief")]),
    ]
    body = transport.build_request(
        model=anthropic_model(),
        messages=messages,
        tools=[],
        system="",
        max_tokens=None,
        thinking_level=None,
        credential_key="k",
        credential_type="api_key",
    ).payload["messages"]

    assert body[0]["role"] == "user"
    assert body[1]["content"][0] == {
        "type": "tool_use",
        "id": "t1",
        "name": "read",
        "input": {"path": "docs/BRIEF.md"},
    }
    assert body[2]["role"] == "user"
    assert body[2]["content"][0]["type"] == "tool_result"
    assert body[2]["content"][0]["tool_use_id"] == "t1"


def test_anthropic_image_block():
    transport = AnthropicMessagesTransport()
    body = transport.build_request(
        model=anthropic_model(),
        messages=[
            Message(
                role="user",
                content=[
                    TextPart(text="what is this"),
                    ImagePart(media_type="image/png", data="AAA"),
                ],
            )
        ],
        tools=[],
        system="",
        max_tokens=None,
        thinking_level=None,
        credential_key="k",
        credential_type="api_key",
    ).payload["messages"]
    assert body[0]["content"][1]["source"]["media_type"] == "image/png"


def test_anthropic_stream_fixture(anthropic_stream_fixture):
    transport = AnthropicMessagesTransport()
    state = _StreamState(model=anthropic_model())
    events = collect(transport, state, anthropic_stream_fixture)

    texts = [e.text for e in events if e.type == "text_delta"]
    thinking = [e.text for e in events if e.type == "thinking_delta"]
    assert texts == ["I will read the file."]
    assert thinking == ["Let me look at the repo."]

    completion = state.completion()
    assert completion.text == "I will read the file."
    assert completion.thinking == "Let me look at the repo."
    assert completion.thinking_signature == "sig-abc"
    assert completion.stop_reason == "tool_use"
    assert len(completion.tool_calls) == 1
    call = completion.tool_calls[0]
    assert call.id == "toolu_1"
    assert call.name == "read"
    assert call.arguments == {"path": "docs/BRIEF.md"}

    assert state.usage.input_tokens == 1234
    assert state.usage.cache_read_tokens == 1000
    assert state.usage.cache_write_tokens == 34
    assert state.usage.output_tokens == 57


def test_anthropic_error_event_becomes_terminal_stop():
    transport = AnthropicMessagesTransport()
    state = _StreamState(model=anthropic_model())
    lines = [
        'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
    ]
    collect(transport, state, lines)
    assert state.stop_reason == "error"
    assert "Overloaded" in state.stop_event().message


# --------------------------------------------------------------------- openai chat


def test_deepseek_compat_flags_are_honoured():
    transport = OpenAICompletionsTransport()
    spec = transport.build_request(
        model=deepseek_model(),
        messages=[Message.text("user", "hi")],
        tools=[],
        system="sys",
        max_tokens=None,
        thinking_level="high",
        credential_key="sk-d",
        credential_type="api_key",
    )
    assert spec.url == "https://api.deepseek.com/chat/completions"
    payload = spec.payload
    # deepseek rejects 'developer' and 'max_completion_tokens'/'store'
    assert payload["messages"][0]["role"] == "system"
    assert payload["max_tokens"] == 384_000
    assert "max_completion_tokens" not in payload
    assert "store" not in payload
    assert payload["reasoning_effort"] == "high"


def test_reasoning_content_replayed_when_required():
    transport = OpenAICompletionsTransport()
    message = Message(
        role="assistant",
        content=[
            __import__("aiden.providers.types", fromlist=["ThinkingPart"]).ThinkingPart(
                text="prior thought"
            ),
            ToolCallPart(
                id="c1", name="read", arguments={"path": "a"}, raw_arguments='{"path":"a"}'
            ),
        ],
    )
    payload = transport.build_request(
        model=deepseek_model(),
        messages=[message],
        tools=[],
        system="",
        max_tokens=None,
        thinking_level=None,
        credential_key="k",
        credential_type="api_key",
    ).payload
    assert payload["messages"][0]["reasoning_content"] == "prior thought"
    assert payload["messages"][0]["tool_calls"][0]["function"]["arguments"] == '{"path":"a"}'


def test_openai_default_uses_developer_role_and_max_completion_tokens():
    model = ModelInfo(
        id="qwen3.8-max",
        provider="opencode-go",
        api="openai-completions",
        base_url="https://opencode.ai/zen/go/v1",
        max_tokens=131_072,
    )
    payload = (
        OpenAICompletionsTransport()
        .build_request(
            model=model,
            messages=[],
            tools=[],
            system="sys",
            max_tokens=None,
            thinking_level=None,
            credential_key="k",
            credential_type="api_key",
        )
        .payload
    )
    assert payload["messages"][0]["role"] == "developer"
    assert payload["max_completion_tokens"] == 131_072
    assert payload["store"] is False


def test_openai_completions_stream_fixture(openai_completions_fixture):
    transport = OpenAICompletionsTransport()
    state = _StreamState(model=deepseek_model())
    events = collect(transport, state, openai_completions_fixture)

    assert [e.text for e in events if e.type == "text_delta"] == ["Let me grep."]
    assert [e.text for e in events if e.type == "thinking_delta"] == ["Checking the tree."]

    completion = state.completion()
    assert completion.stop_reason == "tool_use"
    assert completion.tool_calls[0].name == "grep"
    assert completion.tool_calls[0].arguments == {"pattern": "TODO"}
    assert state.usage.input_tokens == 900
    assert state.usage.output_tokens == 40
    assert state.usage.cache_read_tokens == 128
    assert state.usage.reasoning_tokens == 12


def test_truncated_tool_arguments_are_detected_not_executed():
    """max_tokens mid-tool-call must leave unusable args, and stop_reason=max_tokens."""
    transport = OpenAICompletionsTransport()
    state = _StreamState(model=deepseek_model())
    lines = [
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"edit","arguments":"{\\"path\\":\\"a"}}]},"finish_reason":null}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n',
        "data: [DONE]\n\n",
    ]
    collect(transport, state, lines)
    completion = state.completion()
    assert completion.stop_reason == "max_tokens"
    assert completion.tool_calls[0].arguments == {}  # unparseable → not executable
    assert completion.tool_calls[0].raw_arguments == '{"path":"a'


def test_openai_error_payload_becomes_terminal_stop():
    transport = OpenAICompletionsTransport()
    state = _StreamState(model=deepseek_model())
    collect(
        transport,
        state,
        ['data: {"error":{"message":"insufficient credits","type":"invalid_request_error"}}\n\n'],
    )
    assert state.stop_reason == "error"
    assert "insufficient credits" in state.stop_event().message


# ------------------------------------------------------------------ openai responses


def test_responses_request_shape():
    spec = OpenAIResponsesTransport().build_request(
        model=openai_model(),
        messages=[Message.text("user", "hi")],
        tools=[ToolSpec(name="glob", description="glob")],
        system="sys",
        max_tokens=None,
        thinking_level="medium",
        credential_key="sk-o",
        credential_type="api_key",
    )
    assert spec.url == "https://api.openai.com/v1/responses"
    assert spec.payload["instructions"] == "sys"
    assert spec.payload["max_output_tokens"] == 128_000
    assert spec.payload["tools"][0]["type"] == "function"
    assert spec.payload["reasoning"]["effort"] == "medium"
    assert spec.payload["input"][0] == {"role": "user", "content": "hi"}


def test_responses_tool_result_becomes_function_call_output():
    payload = (
        OpenAIResponsesTransport()
        .build_request(
            model=openai_model(),
            messages=[
                Message(
                    role="assistant",
                    content=[ToolCallPart(id="c1", name="glob", arguments={"p": 1})],
                ),
                Message(role="tool", content=[ToolResultPart(call_id="c1", output="a.md")]),
            ],
            tools=[],
            system="",
            max_tokens=None,
            thinking_level=None,
            credential_key="k",
            credential_type="api_key",
        )
        .payload
    )
    assert payload["input"][0] == {
        "type": "function_call",
        "call_id": "c1",
        "name": "glob",
        "arguments": '{"p": 1}',
    }
    assert payload["input"][1] == {
        "type": "function_call_output",
        "call_id": "c1",
        "output": "a.md",
    }


def test_responses_stream_fixture(openai_responses_fixture):
    transport = OpenAIResponsesTransport()
    state = _StreamState(model=openai_model())
    events = collect(transport, state, openai_responses_fixture)

    assert [e.text for e in events if e.type == "text_delta"] == ["Searching now."]
    assert [e.text for e in events if e.type == "thinking_delta"] == ["Planning the search."]

    completion = state.completion()
    assert completion.stop_reason == "tool_use"
    assert completion.tool_calls[0].id == "call_9"
    assert completion.tool_calls[0].name == "glob"
    assert completion.tool_calls[0].arguments == {"pattern": "**/*.md"}
    assert state.usage.input_tokens == 500
    assert state.usage.cache_read_tokens == 64
    assert state.usage.reasoning_tokens == 7


# --------------------------------------------------------------------------- driver


@pytest.mark.asyncio
async def test_stream_driver_end_to_end_with_fake_response(anthropic_stream_fixture):
    """Full stream() path including retry-free happy path, via a stubbed connection."""
    transport = AnthropicMessagesTransport()

    async def fake_open(spec, model):
        return FakeResponse(anthropic_stream_fixture)

    transport._open_stream = fake_open  # type: ignore[assignment]

    events = []
    async for event in transport.stream(
        model=anthropic_model(),
        messages=[Message.text("user", "read the brief")],
        tools=[],
        system="sys",
        credential="sk-test",
    ):
        events.append(event)

    assert events[0].type == "start"
    assert events[-1].type == "stop"
    assert events[-1].reason == "tool_use"
    assert events[-1].cost_usd >= 0
    assert sum(1 for e in events if e.type == "tool_call_start") == 1


@pytest.mark.asyncio
async def test_stream_reports_auth_failure_as_stop_event():
    from aiden.providers.errors import AuthError

    transport = AnthropicMessagesTransport()

    async def boom(spec, model):
        raise AuthError("bad key", provider="anthropic", model=model.id)

    transport._open_stream = boom  # type: ignore[assignment]

    events = [
        e
        async for e in transport.stream(
            model=anthropic_model(),
            messages=[],
            tools=[],
            system="",
            credential="sk-bad",
        )
    ]
    assert events[-1].type == "stop"
    assert events[-1].reason == "error"
    assert "bad key" in events[-1].message


def test_cost_computation_and_usage_addition():
    from aiden.providers.types import Cost

    cost = Cost(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert cost.of(usage) == pytest.approx(3.0 + 15.0 + 0.3)

    total = Usage(input_tokens=1) + Usage(output_tokens=2)
    assert total.total_tokens == 3


# ------------------------------------------------------- provider-level quirks (regressions)


def test_opencode_session_header_is_sent():
    """Regression: opencode-go rejects requests without x-opencode-session."""
    from aiden.providers.capabilities import OPENCODE_SESSION_HEADER

    model = ModelInfo(
        id="qwen3.8-flash",
        provider="opencode-go",
        api="anthropic-messages",
        base_url="https://opencode.ai/zen/go",
        max_tokens=131_072,
    )
    spec = AnthropicMessagesTransport().build_request(
        model=model,
        messages=[],
        tools=[],
        system="",
        max_tokens=None,
        thinking_level=None,
        credential_key="sk-K",
        credential_type="api_key",
        session_id="sess-123",
    )
    assert spec.headers[OPENCODE_SESSION_HEADER] == "sess-123"


def test_non_opencode_provider_gets_no_session_header():
    spec = AnthropicMessagesTransport().build_request(
        model=anthropic_model(),
        messages=[],
        tools=[],
        system="",
        max_tokens=None,
        thinking_level=None,
        credential_key="k",
        credential_type="api_key",
        session_id="sess-123",
    )
    assert "x-opencode-session" not in spec.headers


@pytest.mark.asyncio
async def test_stream_closes_response_without_async_context_manager(anthropic_stream_fixture):
    """Regression: httpx responses from send(stream=True) are not async context managers."""
    closed = {"value": False}

    class TrackingResponse(FakeResponse):
        async def aclose(self) -> None:
            closed["value"] = True

    transport = AnthropicMessagesTransport()

    async def fake_open(spec, model):
        assert "x-opencode-session" not in spec.headers
        return TrackingResponse(anthropic_stream_fixture)

    transport._open_stream = fake_open  # type: ignore[assignment]

    events = [
        e
        async for e in transport.stream(
            model=anthropic_model(),
            messages=[Message.text("user", "hi")],
            system="",
            credential="sk-test",
        )
    ]
    assert events[-1].type == "stop"
    assert closed["value"] is True


@pytest.mark.asyncio
async def test_stream_auto_generates_session_id_for_opencode():
    from aiden.providers.capabilities import OPENCODE_SESSION_HEADER

    model = ModelInfo(
        id="qwen3.8-flash",
        provider="opencode-go",
        api="openai-completions",
        base_url="https://opencode.ai/zen/go/v1",
        max_tokens=1000,
    )
    seen: dict = {}

    transport = OpenAICompletionsTransport()

    async def fake_open(spec, m):
        seen.update(spec.headers)
        return FakeResponse(
            [
                *sse(
                    {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
                ),
                "data: [DONE]\n\n",
            ]
        )

    transport._open_stream = fake_open  # type: ignore[assignment]

    async for _ in transport.stream(
        model=model, messages=[Message.text("user", "hi")], system="", credential="k"
    ):
        pass
    assert seen.get(OPENCODE_SESSION_HEADER)


def test_reasoning_exhausting_budget_is_reported_not_silent():
    """A cap must leave a trace (research/02 §3): empty text + reasoning tokens."""
    transport = OpenAICompletionsTransport()
    state = _StreamState(model=deepseek_model())
    lines = [
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"thinking hard"},"finish_reason":null}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}],"usage":{"prompt_tokens":10,"completion_tokens":500,"completion_tokens_details":{"reasoning_tokens":500}}}\n\n',
        "data: [DONE]\n\n",
    ]
    collect(transport, state, lines)
    stop = state.stop_event()
    assert stop.reason == "max_tokens"
    assert "reasoning" in stop.message
    assert "500" in stop.message
    assert state.completion().diagnostic == stop.message


def test_truncated_tool_call_marked_for_the_loop():
    """Truncated calls must be flagged so the loop fails them instead of executing."""
    transport = OpenAICompletionsTransport()
    state = _StreamState(model=deepseek_model())
    lines = [
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"edit","arguments":"{\\"path\\":\\"a"}}]},"finish_reason":null}]}\n\n',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n',
    ]
    collect(transport, state, lines)
    call = state.completion().tool_calls[0]
    assert call.truncated is True
    assert "truncated by max_tokens" in state.completion().diagnostic


def test_well_formed_tool_call_not_marked_truncated():
    transport = AnthropicMessagesTransport()
    state = _StreamState(model=anthropic_model())
    collect(transport, state, load_anthropic())
    call = state.completion().tool_calls[0]
    assert call.truncated is False
    assert state.completion().diagnostic == ""


def load_anthropic():
    from ..conftest import load_fixture

    return load_fixture("anthropic_messages.sse")


# ------------------------------------------------- loop-bound client cache (regression)


def test_transport_client_is_recreated_across_event_loops():
    """A cached transport must not reuse an httpx client from a closed event loop."""
    import asyncio

    transport = OpenAICompletionsTransport()

    async def get_client():
        return transport.client()

    async def get_twice():
        # Stable within a single loop, and the same object on repeated calls.
        return transport.client(), transport.client()

    first, _ = asyncio.run(get_twice())
    second, second_again = asyncio.run(get_twice())
    assert first is not second, "client reused across event loops would hang or raise"
    assert second is second_again, "client should be stable within one loop"
    asyncio.run(transport.aclose())


def test_explicit_client_is_never_replaced_by_the_cache():
    """Callers that inject a client own its lifecycle."""
    import asyncio

    import httpx

    injected = httpx.AsyncClient()
    transport = OpenAICompletionsTransport(injected)

    async def get():
        return transport.client()

    assert asyncio.run(get()) is injected
    asyncio.run(injected.aclose())
