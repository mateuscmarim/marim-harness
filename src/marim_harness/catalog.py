"""The OpenRouter model catalog: fetch the list of available models so the
picker can offer them, plus pure helpers to parse and filter that list."""

from dataclasses import dataclass
from typing import Optional

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


@dataclass(frozen=True)
class ModelEntry:
    """One selectable model: its provider id and a human-readable name."""

    id: str
    name: str


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
        entries.append(ModelEntry(id=model_id, name=display))
    entries.sort(key=lambda e: e.id)
    return entries


def filter_entries(entries: list[ModelEntry], query: str) -> list[ModelEntry]:
    """Substring filter over id and name (case-insensitive). Blank query keeps
    everything."""
    q = query.strip().lower()
    if not q:
        return entries
    return [e for e in entries if q in e.id.lower() or q in e.name.lower()]


async def fetch_openrouter_models(
    api_key: Optional[str] = None, timeout: float = 10.0
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
    except Exception:
        return []
