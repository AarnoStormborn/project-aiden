"""Loop-level retry policy.

``providers/errors.py`` *classifies* failures; this module decides what the loop **does**
about them. Keeping the two apart matters because the right response to an overflow
(compact and resend) is not a retry, and the right response to a bad API key (abort) is not
a retry either — only transport faults and rate limits are.

Only the connection phase retries inside the transport (``transport/base.py``); once tokens
have been emitted a retry would duplicate content, so mid-stream failures surface as
``Stop(reason="error")`` and are handled here.

Sources: docs/research/01-harness-anatomy.md §retry/error taxonomy; docs/plan/providers.md §6.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from .providers.errors import (
    AuthError,
    BadRequestError,
    OverflowError_,
    ProviderError,
    RateLimitError,
    TransportError,
)


class Action(StrEnum):
    """What the loop should do with a failure."""

    RETRY = "retry"  # same request, after a delay
    COMPACT = "compact"  # shrink context, then resend (not a retry of the same bytes)
    SURFACE = "surface"  # hand the error to the model as an error tool result
    ABORT = "abort"  # stop the run and report


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3

    def delay_for(self, error: ProviderError, attempt: int) -> float:
        """Seconds to wait before ``attempt`` (1-based). Honors ``retry-after``."""
        retry_after = getattr(error, "retry_after_s", None)
        if retry_after:
            return float(retry_after)
        return min(2.0**attempt, 8.0)


def action_for(error: ProviderError) -> Action:
    """Map a classified error to a loop action."""
    if isinstance(error, OverflowError_):
        return Action.COMPACT
    if isinstance(error, (RateLimitError, TransportError)):
        return Action.RETRY
    if isinstance(error, (AuthError, BadRequestError)):
        return Action.ABORT
    return Action.ABORT if error.retryable is False else Action.RETRY


def should_retry(error: ProviderError, attempt: int, policy: RetryPolicy) -> bool:
    """Whether another call is allowed.

    ``attempt`` is the 0-based index of the call that just failed, so with
    ``max_attempts=3`` we retry after failures 0 and 1 (3 calls total) and stop after 2.
    """
    return action_for(error) is Action.RETRY and (attempt + 1) < policy.max_attempts


async def call_with_retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    on_retry: Callable[[ProviderError, int, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``fn``, retrying only failures whose action is ``RETRY``.

    Non-retryable failures propagate — the loop decides between COMPACT/SURFACE/ABORT via
    :func:`action_for`, rather than this helper silently swallowing them.
    """
    policy = policy or RetryPolicy()
    attempt = 0
    while True:
        try:
            return await fn()
        except ProviderError as error:
            if not should_retry(error, attempt, policy):
                raise
            delay = policy.delay_for(error, attempt) + random.random() * 0.25
            if on_retry is not None:
                on_retry(error, attempt + 1, delay)
            await sleep(delay)
            attempt += 1
