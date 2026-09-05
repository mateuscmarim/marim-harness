"""Chunked NDJSON line reader shared by the external-CLI transports.

A leaf module on purpose: both ``subagents/cli_backend.py`` (the ``claude -p``
stream) and ``codex/rpc.py`` (the app-server JSON-RPC stream) read
newline-delimited JSON off a subprocess pipe, and the codex transport used to
import the reader from ``cli_backend`` — whose package ``__init__`` pulls in
the runner → ``codex_spawn`` → ``codex.approvals`` → ``codex.rpc``, i.e. a
cycle that raised ``ImportError`` for anyone importing
``marim_harness.codex.server`` before ``marim_harness.subagents`` (an
embedder, a probe script). Nothing here imports from the package.
"""

from __future__ import annotations

_READ_CHUNK = 65536


async def iter_ndjson_lines(stream, chunk_size: int = _READ_CHUNK):
    """Yield decoded newline-delimited lines from ``stream`` with no per-line
    length cap.

    ``async for line in stream`` (and ``StreamReader.readline``) caps a line at
    asyncio's 64 KiB buffer limit and raises ``ValueError: Separator is found,
    but chunk is longer than limit`` on anything longer. The Claude CLI emits one
    JSON object per line, and a line carrying a ``tool_result`` with file
    contents (a single Read of a large source file) routinely exceeds 64 KiB — so
    that cap crashed otherwise-fine spawns. Reading raw chunks and splitting on
    ``\\n`` ourselves removes the cap (bounded only by available memory). Decoding
    one complete line at a time is safe: a ``\\n`` byte never falls inside a UTF-8
    multibyte sequence, so no character is split across the boundary. A final
    line with no trailing newline is still yielded."""
    buffer = b""
    while True:
        chunk = await stream.read(chunk_size)
        if not chunk:
            break
        buffer += chunk
        while True:
            nl = buffer.find(b"\n")
            if nl < 0:
                break
            line, buffer = buffer[:nl], buffer[nl + 1 :]
            yield line.decode("utf-8", "replace")
    if buffer:  # a final line with no trailing newline
        yield buffer.decode("utf-8", "replace")
