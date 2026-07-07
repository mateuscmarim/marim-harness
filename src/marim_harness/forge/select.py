"""The single build-time forge-backend decision. Folds the config flag and CLI
availability into one place. Adding a gh backend means one more branch here
(e.g. choose by the origin remote host or which CLI is authenticated)."""

from __future__ import annotations

from pathlib import Path

from .backend import ForgeBackend
from .tea_backend import TeaBackend, tea_available


def select_backend(forge_enabled: bool, root: Path) -> ForgeBackend | None:
    """Return the forge backend to use, or None to attach no forge toolset.

    v1: tea only. Returns None when forge is disabled or tea is unavailable."""
    if not forge_enabled:
        return None
    if tea_available():
        return TeaBackend(root)
    return None
