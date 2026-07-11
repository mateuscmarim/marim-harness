"""The project-trust security predicate.

A leaf module (imports only the stdlib) holding the single source of truth for
"is this project trusted?" — the gate that decides whether project-local
content (``.marim/hooks``, ``.marim/mcp.json``, project-scope plugins,
``.marim/skills``, ``.marim/agents``) may load and execute or be injected into
the model's context. A cloned, untrusted repo can ship any of these; without
this gate, cloning a repo and opening it in marim would be enough for it to
run code or prompt-inject the agent with no user consent.

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

import os

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
