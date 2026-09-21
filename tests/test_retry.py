"""Retry policy tests: classification → loop action mapping."""

from __future__ import annotations

import pytest

from aiden.providers.errors import (
    AuthError,
    BadRequestError,
    OverflowError_,
    ProviderError,
    RateLimitError,
    TransportError,
)
from aiden.retry import Action, RetryPolicy, action_for, call_with_retry, should_retry


def test_overflow_compacts_rather_than_retries():
    assert action_for(OverflowError_("too long")) is Action.COMPACT


def test_rate_limit_and_transport_retry():
    assert action_for(RateLimitError("429")) is Action.RETRY
    assert action_for(TransportError("connection reset")) is Action.RETRY


def test_auth_and_bad_request_abort():
    assert action_for(AuthError("bad key")) is Action.ABORT
    assert action_for(BadRequestError("bad schema")) is Action.ABORT


def test_unknown_provider_error_uses_its_retryable_flag():
    assert action_for(ProviderError("weird", retryable=True)) is Action.RETRY
    assert action_for(ProviderError("weird", retryable=False)) is Action.ABORT


def test_retry_after_is_honored_over_backoff():
    policy = RetryPolicy()
    error = RateLimitError("slow down", retry_after_s=42.0)
    assert policy.delay_for(error, attempt=0) == 42.0


def test_backoff_is_bounded():
    policy = RetryPolicy()
    assert policy.delay_for(TransportError("x"), attempt=0) == 1.0
    assert policy.delay_for(TransportError("x"), attempt=10) == 8.0


def test_should_retry_respects_max_attempts():
    policy = RetryPolicy(max_attempts=3)
    error = TransportError("x")
    assert should_retry(error, attempt=0, policy=policy) is True
    assert should_retry(error, attempt=1, policy=policy) is True
    assert should_retry(error, attempt=2, policy=policy) is False  # 3 calls used up
    # non-retryable actions never retry regardless of attempts left
    assert should_retry(AuthError("x"), attempt=0, policy=policy) is False


@pytest.mark.asyncio
async def test_call_with_retry_succeeds_after_transient_failures():
    attempts = {"n": 0}
    slept: list[float] = []

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransportError("flaky")
        return "ok"

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    result = await call_with_retry(flaky, policy=RetryPolicy(max_attempts=3), sleep=fake_sleep)
    assert result == "ok"
    assert attempts["n"] == 3
    assert len(slept) == 2


@pytest.mark.asyncio
async def test_call_with_retry_propagates_non_retryable_so_the_loop_decides():
    async def bad() -> str:
        raise OverflowError_("context too long")

    with pytest.raises(OverflowError_):
        await call_with_retry(bad, sleep=lambda _s: _noop())


@pytest.mark.asyncio
async def test_call_with_retry_gives_up_after_max_attempts():
    attempts = {"n": 0}

    async def always_fails() -> str:
        attempts["n"] += 1
        raise TransportError("down")

    async def fake_sleep(_seconds: float) -> None:
        return None

    with pytest.raises(TransportError):
        await call_with_retry(always_fails, policy=RetryPolicy(max_attempts=2), sleep=fake_sleep)
    assert attempts["n"] == 2


async def _noop() -> None:
    return None
