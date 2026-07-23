"""Model catalogs for OpenRouter, Google/Gemini, and OpenCode Zen: fetch the
list of available models so the picker can offer them, plus pure helpers to
parse and filter that list."""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
# The current-key endpoint: unlike /models (public), this 401s on a bad key,
# so strict verification probes it to get a real verdict on the credential.
_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
_GOOGLE_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
# Zen serves three API shapes under one roof; marim's zen provider speaks only
# the OpenAI-compatible one, so ids Zen routes to the Anthropic/Google shapes
# are filtered out of the catalog — the picker must not offer a model the
# provider's chat/completions path can't actually drive.
_ZEN_EXCLUDED_PREFIXES = ("claude-", "gemini-")


@dataclass(frozen=True)
class ModelEntry:
    """One selectable model: its provider id and a human-readable name.
    ``supports_images`` is True/False when the catalog states it, else None.
    ``provider`` is the source provider (stamped by MultiModelSource); None for a
    raw single-provider catalog. ``qualified`` is the canonical selectable id."""

    id: str
    name: str
    supports_images: bool | None = None
    provider: str | None = None
    # The model's context window in tokens, when the catalog states it
    # (OpenRouter context_length, Google inputTokenLimit); None when the
    # source doesn't say. Consumed by config.context_limits to derive the
    # compaction/masking threshold.
    context_window: int | None = None
    # Whether the model accepts a reasoning-effort setting, per the catalog
    # (OpenRouter lists "reasoning" in supported_parameters). None when the
    # source doesn't say. Best-effort UI annotation only — NEVER a gate on
    # selecting or applying a thinking level (see the design spec §8).
    supports_thinking: bool | None = None

    @property
    def qualified(self) -> str:
        return f"{self.provider}:{self.id}" if self.provider else self.id


def parse_models(payload: dict) -> list[ModelEntry]:
    """Turn an OpenRouter ``/models`` response into sorted entries, skipping any
    malformed rows. The display name falls back to the id when absent."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    entries: list[ModelEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        name = row.get("name")
        display = name if isinstance(name, str) and name else model_id
        arch = row.get("architecture")
        supports_images: bool | None = None
        if isinstance(arch, dict):
            mods = arch.get("input_modalities")
            if isinstance(mods, list):
                supports_images = "image" in mods
        ctx = row.get("context_length")
        context_window = ctx if isinstance(ctx, int) and ctx > 0 else None
        params = row.get("supported_parameters")
        supports_thinking: bool | None = None
        if isinstance(params, list):
            supports_thinking = "reasoning" in params
        entries.append(ModelEntry(id=model_id, name=display,
                                  supports_images=supports_images,
                                  context_window=context_window,
                                  supports_thinking=supports_thinking))
    entries.sort(key=lambda e: e.id)
    return entries


def filter_entries(entries: list[ModelEntry], query: str) -> list[ModelEntry]:
    """Substring filter over id, name, and provider (case-insensitive). Blank query keeps
    everything."""
    q = query.strip().lower()
    if not q:
        return entries
    return [
        e for e in entries
        if q in e.id.lower()
        or q in e.name.lower()
        or (e.provider is not None and q in e.provider.lower())
    ]


def parse_google_models(payload: dict) -> list[ModelEntry]:
    """Turn a Gemini ``/v1beta/models`` response into sorted entries, keeping
    only models that support generateContent (i.e. chat-capable models)."""
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    entries: list[ModelEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_name = row.get("name", "")
        if not isinstance(raw_name, str) or not raw_name:
            continue
        methods = row.get("supportedGenerationMethods")
        if not isinstance(methods, list) or "generateContent" not in methods:
            continue
        model_id = raw_name.removeprefix("models/")
        display = row.get("displayName") or model_id
        # The Gemini /v1beta/models response doesn't report input modalities, so
        # image support is genuinely unknown here — leave it None rather than
        # asserting True (the contract on ModelEntry.supports_images is
        # "True/False when the catalog states it, else None"). None never blocks
        # submission, same as the OpenRouter parser does for rows lacking the field.
        limit = row.get("inputTokenLimit")
        context_window = limit if isinstance(limit, int) and limit > 0 else None
        entries.append(ModelEntry(id=model_id, name=display, supports_images=None,
                                  context_window=context_window))
    entries.sort(key=lambda e: e.id)
    return entries


async def fetch_google_models(
    api_key: str | None = None, timeout: float = 10.0, *, strict: bool = False
) -> list[ModelEntry]:
    """Fetch the Gemini model catalog. Returns ``[]`` on any failure, unless
    ``strict=True`` (used by verification, which needs the real error instead
    of a silent empty catalog), in which case the exception is re-raised."""
    import httpx

    # Sent as a header, never a query param: httpx's exception str() embeds the
    # full request URL including the query string, so a query-string key would
    # leak into the ✗ badge text and the warning log line below on every failure.
    headers = {"x-goog-api-key": api_key} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(_GOOGLE_MODELS_URL, headers=headers)
            response.raise_for_status()
            return parse_google_models(response.json())
    except Exception as exc:
        if strict:
            raise
        logger.warning("failed to fetch Google model catalog: %s", exc)
        return []


async def fetch_openrouter_models(
    api_key: str | None = None, timeout: float = 10.0, *, strict: bool = False
) -> list[ModelEntry]:
    """Fetch the OpenRouter catalog. Returns ``[]`` on any failure so callers can
    degrade to free-text entry, unless ``strict=True`` (verification needs the
    real error), in which case the exception is re-raised. httpx is imported
    lazily to keep import light."""
    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if strict and api_key:
                # /models is public, so the catalog fetch below succeeds even
                # with a garbage key — it can't validate a credential. /key
                # requires auth (401 on a bad key), so strict verification
                # probes it first. The non-strict picker path never pays this
                # extra request.
                (await client.get(_OPENROUTER_KEY_URL, headers=headers)).raise_for_status()
            response = await client.get(_OPENROUTER_MODELS_URL, headers=headers)
            response.raise_for_status()
            return parse_models(response.json())
    except Exception as exc:
        if strict:
            raise
        logger.warning("failed to fetch OpenRouter model catalog: %s", exc)
        return []


async def fetch_local_models(
    base_url: str | None, api_key: str | None = None, timeout: float = 10.0,
    *, strict: bool = False,
) -> list[ModelEntry]:
    """Fetch the catalog from a local OpenAI-compatible server (LM Studio, Ollama,
    …) by GETting ``{base_url}/models``. The response is the standard OpenAI
    ``{"data": [{"id": ...}]}`` shape, so ``parse_models`` handles it. Returns
    ``[]`` on any failure (or no base_url) so the picker degrades to free-text
    entry, unless ``strict=True`` (verification needs the real error), in which
    case the exception is re-raised. httpx is imported lazily to keep the
    import chain light."""
    if not base_url:
        return []
    import httpx

    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return parse_models(response.json())
    except Exception as exc:
        if strict:
            raise
        logger.warning("failed to fetch local model catalog from %s: %s", url, exc)
        return []


def parse_zen_models(payload: dict) -> list[ModelEntry]:
    """Turn Zen's ``/models`` response (standard OpenAI list shape, id-only —
    no pricing/context metadata) into sorted entries, dropping ids that route
    to non-OpenAI endpoint shapes (see _ZEN_EXCLUDED_PREFIXES)."""
    return [
        e for e in parse_models(payload)
        if not e.id.startswith(_ZEN_EXCLUDED_PREFIXES)
    ]


async def fetch_zen_models(
    api_key: str | None = None, timeout: float = 10.0, *, strict: bool = False
) -> list[ModelEntry]:
    """Fetch the OpenCode Zen catalog. Returns ``[]`` on any failure so the
    picker degrades to free-text entry, unless ``strict=True`` (verification
    needs the real error), in which case the exception is re-raised. Note:
    ``/models`` is public, so strict mode verifies *connectivity*, not the
    key — Zen has no known key-validation endpoint (unlike OpenRouter's
    ``/key``); a bad key surfaces at first chat request instead. httpx is
    imported lazily to keep the import chain light."""
    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(_ZEN_MODELS_URL, headers=headers)
            response.raise_for_status()
            return parse_zen_models(response.json())
    except Exception as exc:
        if strict:
            raise
        logger.warning("failed to fetch OpenCode Zen model catalog: %s", exc)
        return []


def parse_lmstudio_models(payload: dict) -> dict[str, int]:
    """Model id → *served* context window from the enhanced ``/api/v0/models``.

    Trusts only ``loaded_context_length`` — the window the model is *actually
    serving* — and never ``max_context_length`` (what the weights support). The
    two can differ wildly, and the difference is the whole bug: a model whose
    weights support 262k may be loaded to serve only ~101k, so reporting the max
    as the window inflated the compaction threshold to ~0.8x the weights-max
    while the server rejected anything past the smaller served window — requests
    overflowed while the token gauge read 12%. ``loaded_context_length`` only
    exists on rows with ``state: "loaded"``; a row without it is omitted (window
    *unknown*, not "the weights-max"), so the caller falls back to its
    conservative default threshold rather than to an over-optimistic number the
    server will not honor."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    windows: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        ctx = row.get("loaded_context_length")
        if isinstance(ctx, int) and ctx > 0:
            windows[model_id] = ctx
    return windows


async def fetch_lmstudio_windows(
    base_url: str | None, api_key: str | None = None, timeout: float = 10.0
) -> dict[str, int]:
    """Probe LM Studio's ``/api/v0/models`` for per-model context windows.

    The OpenAI-compatible ``/v1/models`` carries no context information, so
    this hits the enhanced REST API instead, derived from the same base_url
    (``…:1234/v1`` → ``…:1234/api/v0/models``). Returns ``{}`` on any failure
    — a non-LM-Studio local server 404s here and that must never break a turn.
    httpx is imported lazily to keep the import chain light."""
    if not base_url:
        return {}
    import httpx

    root = base_url.rstrip("/")
    root = root.removesuffix("/v1")
    url = f"{root}/api/v0/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return parse_lmstudio_models(response.json())
    except Exception as exc:
        logger.warning("failed to fetch LM Studio windows from %s: %s", url, exc)
        return {}


def model_supports_images(entries: list[ModelEntry], model_id: str) -> bool | None:
    """Whether ``model_id`` accepts image input per the catalog; None if the id
    is not present (capability unknown)."""
    for entry in entries:
        if entry.id == model_id:
            return entry.supports_images
    return None


def model_supports_thinking(entries: list[ModelEntry], model_id: str) -> bool | None:
    """Whether ``model_id`` accepts a reasoning-effort setting per the catalog;
    None if the id is not present (capability unknown). Best-effort: a None or
    False here must NOT prevent a user from choosing a thinking level — it only
    annotates the picker."""
    for entry in entries:
        if entry.id == model_id:
            return entry.supports_thinking
    return None
