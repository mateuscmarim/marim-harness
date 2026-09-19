"""Subscription catalog over HTTP, with upstream-owned OAuth authentication.

Pydantic AI 2.45 has no Codex catalog API. Its public ``http_client`` injection
lets us fetch the catalog without implementing auth or invoking the CLI/SDK.
The backend currently requires a Codex protocol version on GET /models. Keep
this compatibility value explicit and wire-tested until upstream owns discovery.
"""

import logging

from httpx2 import AsyncClient

from ..workspace.catalog import ModelEntry
from .codex_subscription import subscription_provider

logger = logging.getLogger(__name__)
CATALOG_CLIENT_VERSION = "0.154.0"


def _entry(row: dict) -> ModelEntry | None:
    model_id = row.get("slug")
    if row.get("visibility") != "list" or not isinstance(model_id, str) or not model_id.strip():
        return None
    name = row.get("display_name")
    modalities = row.get("input_modalities")
    reasoning = row.get("supported_reasoning_levels")
    context = row.get("context_window")
    return ModelEntry(
        id=model_id,
        name=name if isinstance(name, str) and name.strip() else model_id,
        supports_images="image" in modalities if isinstance(modalities, list) else None,
        supports_thinking=bool(reasoning) if isinstance(reasoning, list) else None,
        context_window=context if type(context) is int and context > 0 else None,
    )


def parse_subscription_models(payload: object) -> list[ModelEntry]:
    """Visible, unique models in upstream order; malformed envelopes are failures."""
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Codex catalog response has no models list")
    entries: dict[str, ModelEntry] = {}
    for row in rows:
        if isinstance(row, dict) and (entry := _entry(row)) is not None:
            entries.setdefault(entry.id, entry)
    return list(entries.values())


async def list_subscription_models(
    configured_model: str | None = None, *, strict: bool = False
) -> list[ModelEntry]:
    """Discover models without a generation request; configured selection is fallback.

    The short-lived catalog client is always closed, including auth/parse failures.
    Upstream attaches and refreshes credentials through its public injection seam;
    no API key, private provider methods, or persistent credential writes are used.
    """
    logger.debug("Fetching native Codex subscription catalog")
    try:
        async with AsyncClient(timeout=10.0) as client:
            provider = subscription_provider(http_client=client)
            response = await client.get(
                provider.base_url + "/models",
                params={"client_version": CATALOG_CLIENT_VERSION},
            )
            response.raise_for_status()
            entries = parse_subscription_models(response.json())
        logger.debug("Native Codex catalog returned %d visible models", len(entries))
        if entries or strict:
            return entries
    except Exception as exc:
        # Exceptions from auth/HTTP may contain response bodies. Only log their
        # class, never credentials, headers, or an account's raw payload.
        logger.info("Native Codex catalog unavailable (%s)", type(exc).__name__)
        if strict:
            raise
    logger.debug("Using configured native Codex model as catalog fallback")
    return (
        [ModelEntry(configured_model, configured_model)]
        if configured_model and configured_model.strip()
        else []
    )
