"""The claude-cli model catalog for the picker: the ``models`` list the CLI
returns from its ``initialize`` control response, with a static fallback when
the CLI cannot be launched (not installed, too old, dead) so the picker never
goes blank.

That list is the CLI's own ``/model`` menu — already filtered by the account's
plan and the org's model allowlist, and updated with every Claude Code release
— so marim never has to know which aliases exist or which models the account
may use. Each entry looks like::

    {"value": "opus[1m]", "resolvedModel": "claude-opus-5[1m]",
     "displayName": "Opus (1M context)", "description": "...",
     "supportsEffort": true, "supportedEffortLevels": ["low", ...]}

``value`` is what ``--model`` accepts and is therefore the ``ModelEntry.id``.

Where the list comes from, in order:

1. The cache: every ``ClaudeProcess`` that completes its handshake calls
   ``remember`` with the response, so a session already running on claude-cli
   (or a ``backend: claude-cli`` spawn) has refreshed the catalog for free.
2. A probe: with nothing cached (a ``marim models list``, the picker on a
   session running another provider), one throwaway ``claude`` is launched for
   the handshake alone — no user message, no session file
   (``--no-session-persistence``) — and closed again. Concurrent callers share
   one in-flight probe.

Every entry is ``supports_thinking=True``: marim's thinking level reaches
Claude Code as a thinking-token budget plus an effort level
(``claude/controls.py``), and each Claude model honours one of the two — the
CLI's per-model ``supportsEffort`` only says which, not whether.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from ..workspace.catalog import ModelEntry
from .env import INSTALL_HINT, CliUnavailable, resolve_cli_binary

logger = logging.getLogger(__name__)

PROVIDER = "claude-cli"

# How long a cached list stays fresh without a live process refreshing it.
# The list changes with CLI releases and plan changes, not minute to minute;
# the cost of staleness is one `--model` the CLI then rejects at first use.
CACHE_TTL = 600.0


def _entry(model_id: str, name: str) -> ModelEntry:
    return ModelEntry(id=model_id, name=name, provider=PROVIDER, supports_thinking=True)


# The fallback list: the family aliases every Claude Code release resolves
# (the CLI's own `--model` help names them). Order = the CLI's `/model` menu.
STATIC_MODELS: tuple[ModelEntry, ...] = (
    _entry("opus", "Opus"),
    _entry("sonnet", "Sonnet"),
    _entry("haiku", "Haiku"),
    _entry("fable", "Fable"),
)


def entries_from(models: list[dict]) -> list[ModelEntry]:
    """``initialize.models`` → ``ModelEntry`` list, CLI order kept. The name
    pairs the menu label with the concrete model an alias resolves to (``Opus
    · claude-opus-5``) so a picker row says which version ``opus`` means
    today; an entry without a ``value`` is skipped."""
    out: list[ModelEntry] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        model_id = str(m.get("value") or "")
        if not model_id:
            continue
        name = str(m.get("displayName") or model_id)
        resolved = str(m.get("resolvedModel") or "")
        if resolved and resolved != model_id:
            name = f"{name} · {resolved}"
        out.append(_entry(model_id, name))
    return out


# --- cache -------------------------------------------------------------------

_cached: list[ModelEntry] = []
_cached_at = 0.0
_cached_binary = ""
_inflight: asyncio.Task[list[ModelEntry]] | None = None


def remember(init_response: dict, *, binary: str | None = None) -> None:
    """Record the ``models`` of one ``initialize`` response. Called by every
    ``ClaudeProcess`` handshake; a response without a usable list (an old CLI,
    a truncated reply) leaves the cache untouched rather than emptying it."""
    global _cached, _cached_at, _cached_binary
    models = init_response.get("models")
    if not isinstance(models, list):
        return
    entries = entries_from(models)
    if not entries:
        return
    _cached = entries
    _cached_at = time.monotonic()
    _cached_binary = binary or resolve_cli_binary() or ""


def cached_models(*, binary: str | None = None) -> list[ModelEntry] | None:
    """The remembered list while fresh and recorded for the same binary
    (a ``MARIM_CLAUDE_CLI_BIN`` change must not serve another CLI's menu);
    None otherwise."""
    if not _cached or time.monotonic() - _cached_at > CACHE_TTL:
        return None
    if binary is not None and _cached_binary and binary != _cached_binary:
        return None
    return list(_cached)


def reset() -> None:
    """Forget the cache (tests)."""
    global _cached, _cached_at, _cached_binary, _inflight
    _cached, _cached_at, _cached_binary, _inflight = [], 0.0, "", None


# --- probe -------------------------------------------------------------------


async def _probe(binary: str) -> list[ModelEntry]:
    """Launch one ``claude`` for the handshake alone and read its ``models``.
    Imported lazily: ``process`` calls ``remember`` on every handshake, and
    this module must stay importable from there."""
    from .process import ClaudeProcess, ProcessOptions

    process = ClaudeProcess(ProcessOptions(binary=binary, cwd=os.getcwd(), persist=False))
    try:
        await process.start()
        entries = entries_from(process.init_result.get("models") or [])
    finally:
        await process.aclose()
    if entries:
        remember({"models": process.init_result.get("models")}, binary=binary)
    return entries


async def _shared_probe(binary: str) -> list[ModelEntry]:
    """One probe at a time per event loop: a second caller arriving while a
    probe is in flight awaits that probe instead of launching its own CLI."""
    global _inflight
    loop = asyncio.get_running_loop()
    task = _inflight
    if task is None or task.done() or task.get_loop() is not loop:
        task = _inflight = loop.create_task(_probe(binary))
    try:
        return await asyncio.shield(task)
    finally:
        if _inflight is task and task.done():
            _inflight = None


async def list_claude_models(*, strict: bool = False) -> list[ModelEntry]:
    """Live catalog (cache, else probe), or ``STATIC_MODELS`` on any failure.
    ``strict=True`` re-raises instead — provider verification needs to tell
    "connected, 0 models" apart from "failed to connect" — and an empty live
    list is then returned as-is."""
    binary = resolve_cli_binary()
    try:
        if binary is None:
            raise CliUnavailable(f"claude CLI not found. {INSTALL_HINT}")
        entries = cached_models(binary=binary)
        if entries is None:
            entries = await _shared_probe(binary)
        return entries if (entries or strict) else list(STATIC_MODELS)
    except Exception as exc:
        if strict:
            raise
        logger.info("claude model catalog unavailable (%s); using the static catalog", exc)
        return list(STATIC_MODELS)
