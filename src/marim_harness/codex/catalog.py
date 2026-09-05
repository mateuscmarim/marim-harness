"""The codex-cli model catalog for the picker: ``model/list`` from the shared
app-server, with a static fallback when the server is unavailable (not
installed, not logged in, or dead) so the picker never goes blank.

Every Codex model accepts a reasoning effort, so ``supports_thinking`` is
True for all entries (spec §Model catalog). Context windows are not reported
by ``model/list`` — ``context_limits`` falls back to its defaults.
"""

from __future__ import annotations

import logging

from ..workspace.catalog import ModelEntry
from .server import CodexServer, shared_server

logger = logging.getLogger(__name__)

PROVIDER = "codex-cli"


def _entry(model_id: str, name: str) -> ModelEntry:
    return ModelEntry(id=model_id, name=name, provider=PROVIDER, supports_thinking=True)


# The fallback list. Keep in sync with Task 0's `codex app-server` model/list
# output at the pinned MIN_CODEX_VERSION; order = the CLI's own (default first).
STATIC_MODELS: tuple[ModelEntry, ...] = (
    _entry("gpt-5.6-sol", "GPT-5.6 Sol"),
    _entry("gpt-5.6", "GPT-5.6"),
    _entry("gpt-5.6-codex", "GPT-5.6 Codex"),
    _entry("gpt-5.5", "GPT-5.5"),
    _entry("gpt-5.4-mini", "GPT-5.4 mini"),
    _entry("gpt-5.3-codex", "GPT-5.3 Codex"),
)


def entries_from(models: list[dict]) -> list[ModelEntry]:
    out: list[ModelEntry] = []
    for m in models:
        model_id = str(m.get("id") or m.get("model") or "")
        if not model_id:
            continue
        out.append(_entry(model_id, str(m.get("displayName") or model_id)))
    return out


async def list_codex_models(
    *, strict: bool = False, server: CodexServer | None = None
) -> list[ModelEntry]:
    """Live catalog, or ``STATIC_MODELS`` on any failure (``strict=True``
    re-raises instead — provider verification needs to tell "connected, 0
    models" from "failed to connect")."""
    srv = server if server is not None else shared_server()
    try:
        await srv.start()
        entries = entries_from(await srv.list_models())
        # An empty *live* response is meaningful under strict=True (the caller
        # needs to tell "connected, 0 models" apart from "failed to connect"),
        # so only the non-strict path falls back to the static catalog here.
        return entries if (entries or strict) else list(STATIC_MODELS)
    except Exception as exc:
        if strict:
            raise
        logger.info("codex model/list unavailable (%s); using the static catalog", exc)
        return list(STATIC_MODELS)
