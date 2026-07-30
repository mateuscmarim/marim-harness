"""The first-open project-trust prompt.

Free functions rather than a collaborator object: this runs at most once per
session, holds no state between the two steps, and the App only needs to fire
it from on_mount. See ``trust.py`` / ``docs/guides/trust.md`` for what a trust
decision actually gates.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...trust import record_decision
from .interactions import TrustPanel, run_panel

if TYPE_CHECKING:
    from .app import HarnessApp

logger = logging.getLogger(__name__)


async def prompt_project_trust(app: HarnessApp) -> None:
    """First-open trust dialog. Failure to persist must not strand the
    decision: the session still applies it (the user consented), the error
    is surfaced as a system line."""
    surface = app.harness.trust_prompt
    if surface is None:  # pragma: no cover - guarded by the on_mount check
        return
    trusted = bool(await run_panel(app, TrustPanel(surface)))
    try:
        record_decision(
            app.harness.deps.workspace.root, trusted=trusted,
            fingerprint=surface.fingerprint,
            now=datetime.now(timezone.utc).isoformat(),
        )
    except OSError as exc:
        await app.post_system(f"Couldn't save the trust decision: {exc}")
    if trusted:
        await apply_trust_and_confirm(app)
    else:
        await app.post_system(
            "Project config present but not trusted — `/trust on` to enable."
        )


async def apply_trust_and_confirm(app: HarnessApp) -> None:
    """Hot-apply the just-granted trust (hooks reload, MCP config load, LSP
    registry rebuild) and confirm. The decision is already persisted and
    the TrustState already flipped by this point — the user consented, so
    neither is undone if the hot-apply itself blows up; a failure here just
    means a restart is needed to pick up the config, not that anything was
    rolled back. Runs inside a worker with exit_on_error=False and no
    on_worker_state_changed handler, so an unguarded raise here would
    otherwise vanish silently (same belt-and-suspenders as
    the shell passthrough)."""
    try:
        await app.harness.apply_project_trust()
    except Exception as exc:  # keep the session alive on any hot-apply failure
        await app.post_system(
            "Project trusted and saved, but applying it live failed: "
            f"{type(exc).__name__}: {exc}. Restart marim to pick up the config."
        )
        logger.warning("apply_project_trust failed", exc_info=True)
        return
    await app.post_system("Project trusted — hooks, MCP, skills and agents are live.")
