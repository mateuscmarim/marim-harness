"""Claude-Code-compatible lifecycle-hook engine. See
docs/superpowers/specs/2026-06-17-marim-hooks-engine-design.md."""

from .config import load_hooks_config
from .runner import HookRunner, base_payload

__all__ = ["load_hooks_config", "HookRunner", "base_payload"]
