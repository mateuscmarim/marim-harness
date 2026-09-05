"""Codex app-server transport: the `codex-cli` provider and sub-agent backend.

Deliberately re-exports nothing — import submodules directly (``from
.server import CodexServer``) so config/ and subagents/ can import pieces of
this package lazily without pulling the subprocess machinery at import time.
"""
