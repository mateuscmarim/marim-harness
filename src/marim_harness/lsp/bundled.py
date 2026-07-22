"""Load marim's four bundled LSP language plugins from in-tree manifests.

These ship inside the wheel and are always trusted (shipped with marim). They
reuse the exact ``lsp`` manifest schema third-party plugins use, but bypass the
install/registry/trust discovery machinery — they are marim's defaults, always
on. Bundled manifests may use the bundled-only ``backend``/``diagnostics`` keys.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .provider import LspProvider, parse_lsp_providers

logger = logging.getLogger(__name__)


def bundled_lsp_dir() -> Path:
    return Path(__file__).resolve().parent / "bundled"


def bundled_lsp_providers() -> list[LspProvider]:
    """All bundled language providers, parsed strictly (a broken bundled
    manifest is a marim packaging bug, so surface it via a warning and skip)."""
    out: list[LspProvider] = []
    root = bundled_lsp_dir()
    try:
        subdirs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        return out
    for d in subdirs:
        manifest = d / ".marim-plugin" / "plugin.json"
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("skipping bundled lsp plugin at %s: %s", d, exc)
            continue
        block = raw.get("lsp") if isinstance(raw, dict) else None
        if block is None:
            continue
        try:
            out.extend(
                parse_lsp_providers(
                    block, bundled=True, source="bundled",
                    plugin_root=d, strict=True,
                )
            )
        except Exception as exc:  # noqa: BLE001 — bad bundled manifest ⇒ skip
            logger.warning("invalid bundled lsp plugin at %s: %s", d, exc)
    return out
