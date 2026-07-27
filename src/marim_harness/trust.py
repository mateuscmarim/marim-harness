"""The project-trust security predicate.

A leaf module (imports only the stdlib plus the package's own ``atomic_io``)
holding the single source of truth for "is this project trusted?" — the gate
that decides whether project-local content (``.marim/hooks``, ``.marim/mcp.json``,
project-scope plugins, ``.marim/skills``, ``.marim/agents``) may load and execute
or be injected into the model's context. A cloned, untrusted repo can ship any
of these; without this gate, cloning a repo and opening it in marim would be
enough for it to run code or prompt-inject the agent with no user consent.

Semantics: an explicit caller decision (typically threaded from
``cfg.trust_project_hooks``) always wins. Absent one, we fall back to the
``MARIM_TRUST_PROJECT_HOOKS`` env var, read here rather than threaded so the
gate still holds at the several call sites that don't carry an explicit flag
through (instructions/provider/tui/runner). Absent *both*, the project is
untrusted — fail closed. This is safe by default: a project's own ``.env`` is
forbidden from setting that key (see ``config/env._PROJECT_ENV_BLOCKLIST``),
so a cloned repo cannot self-trust — the value comes only from the real shell
env or the user's global config.

This predicate used to be copy-pasted across ``workspace/skills.py``,
``workspace/agents.py``, and ``plugins/discovery.py``. Three copies of a
security predicate is how they quietly drift out of sync — one gets a fix the
others don't, and the gate stops meaning the same thing everywhere it's
checked. Import from here instead of re-implementing it.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import atomic_write_text, file_lock

logger = logging.getLogger(__name__)

# Truthy spellings for MARIM_TRUST_PROJECT_HOOKS.
_TRUTHY = {"1", "true", "on", "yes"}


def project_trusted(explicit: bool | None = None) -> bool:
    """Whether the current project is trusted to load/execute project-local
    content. ``explicit`` (an already-resolved caller decision) wins when
    given; otherwise falls back to the ``MARIM_TRUST_PROJECT_HOOKS`` env var;
    unset or unrecognized ⇒ untrusted."""
    if explicit is not None:
        return explicit
    return os.getenv("MARIM_TRUST_PROJECT_HOOKS", "").strip().lower() in _TRUTHY


def trust_env() -> bool | None:
    """Tri-state read of ``MARIM_TRUST_PROJECT_HOOKS``: None when unset or
    blank (no decision — fall through to the store), True for a truthy
    spelling, False for anything else (an explicit falsy value force-untrusts,
    overriding even a trusting store entry)."""
    raw = os.getenv("MARIM_TRUST_PROJECT_HOOKS")
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in _TRUTHY


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "marim-harness"


def trusted_projects_path() -> Path:
    """The per-machine trust store. State, not data: operator decisions about
    local checkouts — never inside the repo (a repo must not self-trust) and
    not synced content."""
    return _state_dir() / "trusted-projects.json"


@dataclass(frozen=True)
class StoredDecision:
    trusted: bool
    fingerprint: str
    decided_at: str


def _load_store() -> dict:
    """The whole store mapping, or {} on any read problem — a broken store
    fails CLOSED (everything untrusted), never fatal."""
    path = trusted_projects_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        logger.warning("trust store unreadable, treating as empty: %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def stored_decision(workspace_root) -> StoredDecision | None:
    """The remembered decision for ``workspace_root`` (resolved), or None.
    Malformed entries read as absent — fail closed."""
    entry = _load_store().get(str(Path(workspace_root).resolve()))
    if not isinstance(entry, dict) or not isinstance(entry.get("trusted"), bool):
        return None
    return StoredDecision(
        trusted=entry["trusted"],
        fingerprint=str(entry.get("fingerprint", "")),
        decided_at=str(entry.get("decided_at", "")),
    )


def record_decision(workspace_root, *, trusted: bool, fingerprint: str, now: str) -> None:
    """Persist a decision for ``workspace_root``. Read-modify-write under the
    same advisory lock discipline as the plugin registry so two concurrent
    sessions can't clobber each other's entries."""
    path = trusted_projects_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        store = _load_store()
        store[str(Path(workspace_root).resolve())] = {
            "trusted": trusted, "fingerprint": fingerprint, "decided_at": now,
        }
        atomic_write_text(path, json.dumps(store, indent=2, sort_keys=True))


@dataclass(frozen=True)
class TrustResolution:
    """The outcome of full store-aware trust resolution. ``source`` names the
    layer that decided (config/env/store/default) — surfaced by /trust, the
    settings row, `marim trust`, and GET /v1/.../trust. ``prompt_needed`` is
    True only in the one state where an interactive front-end should ask:
    no decision anywhere and a non-empty gated surface."""

    trusted: bool
    source: str
    prompt_needed: bool


def resolve_project_trust(
    workspace_root, *, explicit: bool | None, fingerprint: str, surface_empty: bool
) -> TrustResolution:
    """Store-aware trust resolution: explicit caller decision → env var →
    stored decision (honored only while its fingerprint matches the current
    executable surface) → untrusted. The leaf predicate ``project_trusted``
    can't do this — it doesn't know the workspace root — so bootstrap/builder
    call this once to seed the session's TrustState, and the trust front-ends
    re-call it on change."""
    if explicit is not None:
        return TrustResolution(explicit, "config", False)
    env = trust_env()
    if env is not None:
        return TrustResolution(env, "env", False)
    stored = stored_decision(workspace_root)
    if stored is not None and stored.fingerprint == fingerprint:
        return TrustResolution(stored.trusted, "store", False)
    # No usable decision: untrusted, and worth prompting only when the project
    # actually ships gated content (a stale-fingerprint entry lands here too —
    # the surface changed since the last decision, so re-ask).
    return TrustResolution(False, "default", not surface_empty)
