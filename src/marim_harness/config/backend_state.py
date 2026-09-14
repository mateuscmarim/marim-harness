"""Best-effort reads of optional CLI display snapshots."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def backend_snapshot(model: object, name: str) -> dict:
    try:
        value = getattr(model, name, {})
        return dict(value) if isinstance(value, dict) else {}
    except Exception as exc:
        logger.warning("%s %s refresh failed (%s)", type(model).__name__, name, type(exc).__name__)
        return {}
