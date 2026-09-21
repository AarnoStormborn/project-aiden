from __future__ import annotations

import pytest

from aiden.providers.sse import SSEParser, iter_sse_lines


def test_single_event():
    events = iter_sse_lines(['event: ping\ndata: {"a":1}\n\n'])
    assert len(events) == 1
    assert events[0].event == "ping"
    assert events[0].json() == {"a": 1}


def test_multi_line_data_joined_with_newline():
    events = iter_sse_lines(["data: line one\n", "data: line two\n", "\n"])
    assert events[0].data == "line one\nline two"


def test_comments_and_unknown_fields_ignored():
    events = iter_sse_lines([": keep-alive\n", "retry: 5000\n", "data: x\n", "\n"])
    assert len(events) == 1
    assert events[0].data == "x"


def test_chunk_boundary_splits_mid_field():
    parser = SSEParser()
    assert parser.feed('data: {"hel') == []
    assert parser.feed('lo": 1}') == []
    events = parser.feed("\n\n")
    assert len(events) == 1
    assert events[0].json() == {"hello": 1}


def test_chunk_boundary_inside_crlf():
    parser = SSEParser()
    parser.feed("data: abc\r")
    events = parser.feed("\n\r\n")
    assert [e.data for e in events] == ["abc"]


def test_value_leading_space_stripped_once():
    events = iter_sse_lines(["data:  two spaces\n", "\n"])
    # SSE spec strips exactly one leading space.
    assert events[0].data == " two spaces"


def test_finish_flushes_unterminated_event():
    parser = SSEParser()
    parser.feed("data: trailing")
    events = parser.finish()
    assert [e.data for e in events] == ["trailing"]


def test_multiple_events_in_one_chunk():
    events = iter_sse_lines(["data: a\n\n", "data: b\n\n"])
    assert [e.data for e in events] == ["a", "b"]


def test_done_sentinel_preserved_verbatim():
    events = iter_sse_lines(["data: [DONE]\n\n"])
    assert events[0].data == "[DONE]"


@pytest.mark.asyncio
async def test_async_iter_sse_roundtrip():
    from aiden.providers.sse import iter_sse

    async def chunks():
        for piece in ['data: {"n":1}\n', "\n", "data: [DONE]\n\n"]:
            yield piece

    seen = [event.data async for event in iter_sse(chunks())]
    assert seen == ['{"n":1}', "[DONE]"]
