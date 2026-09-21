"""Typed provider failures.

The retry policy is *not* here; this module only classifies. Callers (retry.py, the loop)
decide what to do, and every error retains the raw body so the model or the log can see it.

Classification is driven by status code first, then by message pattern — matching the
finding in research/01 that overflow detection is string-matching almost everywhere, so we
keep one small pattern table instead of pretending status codes are sufficient.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Seeded from Pi's ~30-provider overflow table (research/01 §context, research/02 §3).
_OVERFLOW_PATTERNS = (
    re.compile(r"context[_ ]length", re.I),
    re.compile(r"context window", re.I),
    re.compile(r"too many tokens", re.I),
    re.compile(r"maximum context", re.I),
    re.compile(r"prompt is too long", re.I),
    re.compile(r"input is too long", re.I),
    re.compile(r"exceeds the maximum", re.I),
    re.compile(r"reduce the length", re.I),
    re.compile(r"tokens?\s*>\s*\d+", re.I),
)

_RATE_LIMIT_PATTERNS = (
    re.compile(r"rate.?limit", re.I),
    re.compile(r"too many requests", re.I),
    re.compile(r"overloaded", re.I),
    re.compile(r"quota", re.I),
    re.compile(r"insufficient credits", re.I),  # commandcode/opencode-go style
)


@dataclass
class ProviderError(Exception):
    message: str
    provider: str = ""
    model: str = ""
    status: int | None = None
    body: str = ""
    retryable: bool = False
    kind: str = "provider"

    def __str__(self) -> str:  # pragma: no cover - trivial
        bits = [self.kind]
        if self.status:
            bits.append(f"HTTP {self.status}")
        where = f"{self.provider}/{self.model}" if self.provider else ""
        return f"{'/'.join(bits)}: {self.message}" + (f" [{where}]" if where else "")


class AuthError(ProviderError):
    def __init__(self, message: str = "authentication failed", **kw):
        kw.setdefault("status", None)
        super().__init__(message, retryable=False, kind="auth", **kw)


class BadRequestError(ProviderError):
    def __init__(self, message: str, **kw):
        kw.setdefault("status", 400)
        super().__init__(message, retryable=False, kind="bad_request", **kw)


class RateLimitError(ProviderError):
    def __init__(self, message: str = "rate limited", retry_after_s: float | None = None, **kw):
        super().__init__(message, retryable=True, kind="rate_limit", **kw)
        self.retry_after_s = retry_after_s


class OverflowError_(ProviderError):
    """Context-window overflow: correct response is compaction, not retry."""

    def __init__(self, message: str = "context overflow", **kw):
        super().__init__(message, retryable=False, kind="overflow", **kw)


class TransportError(ProviderError):
    def __init__(self, message: str, **kw):
        super().__init__(message, retryable=True, kind="transport", **kw)


class UnsupportedProtocolError(ProviderError):
    def __init__(self, api: str, **kw):
        super().__init__(
            f"no transport implemented for api '{api}'",
            retryable=False,
            kind="unsupported_protocol",
            **kw,
        )


def classify(
    status: int | None,
    body: str,
    *,
    provider: str = "",
    model: str = "",
    retry_after_s: float | None = None,
) -> ProviderError:
    """Map an HTTP status + body to the most specific error we can justify."""
    snippet = _extract_message(body)
    # Any-typed: this dict is splatted into subclasses whose __init__ signatures differ
    # (e.g. RateLimitError takes retry_after_s), so a precise value union would fight them.
    common: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "status": status,
        "body": body[:8000],
    }

    if status in (401, 403):
        return AuthError(snippet or "authentication failed", **common)

    if status == 429 or (
        status is not None and any(p.search(snippet) for p in _RATE_LIMIT_PATTERNS)
    ):
        return RateLimitError(snippet or "rate limited", retry_after_s=retry_after_s, **common)

    if any(p.search(snippet) for p in _OVERFLOW_PATTERNS):
        return OverflowError_(snippet or "context overflow", **common)

    if status == 400:
        return BadRequestError(snippet or "bad request", **common)

    if status is None or status >= 500:
        return TransportError(snippet or f"transport failure ({status})", **common)

    return ProviderError(snippet or f"HTTP {status}", **common)


def _extract_message(body: str) -> str:
    """Pull a human-readable message out of common provider error envelopes."""
    if not body:
        return ""
    import json

    try:
        data = json.loads(body)
    except Exception:
        return body[:500].strip()

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("detail")
            if isinstance(msg, str):
                return msg
        if isinstance(err, str):
            return err
        for key in ("message", "detail", "error_description"):
            val = data.get(key)
            if isinstance(val, str):
                return val
    return body[:500].strip()
