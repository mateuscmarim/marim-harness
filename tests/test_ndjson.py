"""The NDJSON reader is a leaf module: the codex transport must be importable
first in a fresh interpreter, and the reader keeps its no-line-cap contract."""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from marim_harness.ndjson import iter_ndjson_lines

pytestmark = pytest.mark.anyio


def test_codex_server_is_importable_before_subagents():
    """Regression: ``codex/rpc.py`` imported the reader from
    ``subagents.cli_backend``, whose package ``__init__`` loads the runner →
    ``codex_spawn`` → ``codex.approvals`` → ``codex.rpc`` (half-initialised),
    so ``import marim_harness.codex.server`` as the FIRST marim import raised
    ImportError — for an embedder, or the live probe scripts."""
    proc = subprocess.run(
        [sys.executable, "-c", "import marim_harness.codex.server"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


async def test_iter_ndjson_lines_splits_and_keeps_unterminated_tail():
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"a":1}\n' + b"x" * 70000 + b'\n{"tail":true}')
    reader.feed_eof()
    lines = [line async for line in iter_ndjson_lines(reader, chunk_size=1024)]
    assert lines[0] == '{"a":1}'
    assert len(lines[1]) == 70000  # beyond asyncio's 64 KiB readline cap
    assert lines[2] == '{"tail":true}'
