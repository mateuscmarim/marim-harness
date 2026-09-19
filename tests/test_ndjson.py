"""The shared NDJSON reader keeps its no-line-cap contract."""

from __future__ import annotations

import asyncio

import pytest

from marim_harness.ndjson import iter_ndjson_lines

pytestmark = pytest.mark.anyio


async def test_iter_ndjson_lines_splits_and_keeps_unterminated_tail():
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"a":1}\n' + b"x" * 70000 + b'\n{"tail":true}')
    reader.feed_eof()
    lines = [line async for line in iter_ndjson_lines(reader, chunk_size=1024)]
    assert lines[0] == '{"a":1}'
    assert len(lines[1]) == 70000  # beyond asyncio's 64 KiB readline cap
    assert lines[2] == '{"tail":true}'
