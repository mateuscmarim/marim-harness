"""Model catalogs for OpenRouter and Google/Gemini: fetch the list of
available models so the picker can offer them, plus pure helpers to parse
and filter that list."""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_GOOGLE_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


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
        entries.append(ModelEntry(id=model_id, name=display,
                                  supports_images=supports_images))
    entries.sort(key=lambda e: e.id)
    return entries


def filter_entries(entries: list[ModelEntry], query: str) -> list[ModelEntry]:
    """Substring filter over id and name (case-insensitive). Blank query keeps
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
        entries.append(ModelEntry(id=model_id, name=display, supports_images=None))
    entries.sort(key=lambda e: e.id)
    return entries


async def fetch_google_models(
    api_key: str | None = None, timeout: float = 10.0
) -> list[ModelEntry]:
    """Fetch the Gemini model catalog. Returns ``[]`` on any failure."""
    import httpx

    params = {"key": api_key} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(_GOOGLE_MODELS_URL, params=params)
            response.raise_for_status()
            return parse_google_models(response.json())
    except Exception as exc:
        logger.warning("failed to fetch Google model catalog: %s", exc)
        return []


async def fetch_openrouter_models(
    api_key: str | None = None, timeout: float = 10.0
) -> list[ModelEntry]:
    """Fetch the OpenRouter catalog. Returns ``[]`` on any failure so callers can
    degrade to free-text entry. httpx is imported lazily to keep import light."""
    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(_OPENROUTER_MODELS_URL, headers=headers)
            response.raise_for_status()
            return parse_models(response.json())
    except Exception as exc:
        logger.warning("failed to fetch OpenRouter model catalog: %s", exc)
        return []


async def fetch_local_models(
    base_url: str | None, api_key: str | None = None, timeout: float = 10.0
) -> list[ModelEntry]:
    """Fetch the catalog from a local OpenAI-compatible server (LM Studio, Ollama,
    …) by GETting ``{base_url}/models``. The response is the standard OpenAI
    ``{"data": [{"id": ...}]}`` shape, so ``parse_models`` handles it. Returns
    ``[]`` on any failure (or no base_url) so the picker degrades to free-text
    entry. httpx is imported lazily to keep the import chain light."""
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
        logger.warning("failed to fetch local model catalog from %s: %s", url, exc)
        return []


def model_supports_images(entries: list[ModelEntry], model_id: str) -> bool | None:
    """Whether ``model_id`` accepts image input per the catalog; None if the id
    is not present (capability unknown)."""
    for entry in entries:
        if entry.id == model_id:
            return entry.supports_images
    return None
