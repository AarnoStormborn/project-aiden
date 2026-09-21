"""Incremental Server-Sent Events parser.

Anthropic and OpenAI-compatible endpoints both stream SSE, but with different event
vocabularies. This parser only handles the *framing* (§9.2.6 of the HTML spec, simplified):
lines, ``event:`` / ``data:`` / ``id:`` / comments, multi-line data joined with ``\\n``,
blank line terminates an event.

It is fed arbitrary byte chunk boundaries, which is the part hand-rolled parsers get wrong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass


@dataclass(slots=True)
class SSEEvent:
    event: str = ""
    data: str = ""
    id: str = ""

    def json(self):
        import json

        return json.loads(self.data)


class SSEParser:
    """Feed-any-chunk SSE decoder."""

    def __init__(self) -> None:
        self._buffer = ""
        self._event = ""
        self._data: list[str] = []
        self._id = ""

    def feed(self, chunk: str) -> list[SSEEvent]:
        events: list[SSEEvent] = []
        self._buffer += chunk

        while True:
            newline = self._buffer.find("\n")
            if newline == -1:
                break
            line = self._buffer[:newline]
            self._buffer = self._buffer[newline + 1 :]
            if line.endswith("\r"):
                line = line[:-1]

            if line == "":
                if self._data or self._event:
                    events.append(
                        SSEEvent(
                            event=self._event,
                            data="\n".join(self._data),
                            id=self._id,
                        )
                    )
                self._event = ""
                self._data = []
                self._id = ""
                continue

            if line.startswith(":"):
                continue  # comment / keep-alive

            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]

            if field == "event":
                self._event = value
            elif field == "data":
                self._data.append(value)
            elif field == "id":
                self._id = value
            # other fields (retry) are ignored

        return events

    def finish(self) -> list[SSEEvent]:
        """Flush a trailing event that arrived without a terminating blank line."""
        if self._buffer.strip():
            line = self._buffer
            self._buffer = ""
            field, _, value = line.partition(":")
            if field == "data":
                self._data.append(value[1:] if value.startswith(" ") else value)
            elif field == "event":
                self._event = value[1:] if value.startswith(" ") else value
        if self._data or self._event:
            event = SSEEvent(event=self._event, data="\n".join(self._data), id=self._id)
            self._event, self._data, self._id = "", [], ""
            return [event]
        return []


async def iter_sse(chunks: AsyncIterator[str]) -> AsyncIterator[SSEEvent]:
    """Wrap an async iterator of text chunks into a stream of SSE events."""
    parser = SSEParser()
    async for chunk in chunks:
        for event in parser.feed(chunk):
            yield event
    for event in parser.finish():
        yield event


def iter_sse_lines(lines: Iterable[str]) -> list[SSEEvent]:
    """Synchronous helper for tests and recorded fixtures."""
    parser = SSEParser()
    out: list[SSEEvent] = []
    for line in lines:
        out.extend(parser.feed(line if line.endswith("\n") else line + "\n"))
    out.extend(parser.finish())
    return out
