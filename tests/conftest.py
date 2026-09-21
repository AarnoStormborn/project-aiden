"""Shared test helpers: fixture loading and a fake streaming HTTP response."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> list[str]:
    """Read a recorded SSE fixture as a list of lines (newlines preserved)."""
    return (FIXTURES / name).read_text().splitlines(keepends=True)


class FakeResponse:
    """Minimal stand-in for ``httpx.Response`` in streaming mode."""

    def __init__(self, chunks: list[str], status_code: int = 200, headers: dict | None = None):
        self._chunks = chunks
        self.status_code = status_code
        self.headers = headers or {}

    async def aiter_text(self) -> AsyncIterator[str]:
        for chunk in self._chunks:
            yield chunk

    async def aread(self) -> bytes:
        return "".join(self._chunks).encode()

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc) -> None:
        return None


def sse(*payloads: dict | str, event: str | None = None) -> list[str]:
    """Build SSE text from payload dicts (as JSON) or raw strings."""
    out: list[str] = []
    for payload in payloads:
        if event:
            out.append(f"event: {event}\n")
        if isinstance(payload, str):
            out.append(f"data: {payload}\n")
        else:
            out.append(f"data: {json.dumps(payload)}\n")
        out.append("\n")
    return out


@pytest.fixture
def anthropic_stream_fixture() -> list[str]:
    return load_fixture("anthropic_messages.sse")


@pytest.fixture
def openai_completions_fixture() -> list[str]:
    return load_fixture("openai_completions.sse")


@pytest.fixture
def openai_responses_fixture() -> list[str]:
    return load_fixture("openai_responses.sse")
