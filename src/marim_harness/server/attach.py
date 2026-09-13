"""Launch-time discovery for attaching a TUI to a daemon-owned session
(phase 4a).

``marim --resume`` / ``marim --session <id>`` on a session the daemon already
claims used to refuse ("already open in daemon (pid N) at http://…"). This
module turns that refusal into an attach decision, and nothing more: it reads
the claim sidecar, probes the daemon it names, reads the daemon's token file
and matches the workspace against the daemon's registry. Every step is a
small local read or a ≤1s HTTP GET through ``urllib`` — ``default_cmd``
defers heavy imports deliberately, so this module imports neither httpx nor
pydantic-ai.

Attach happens ONLY when the daemon already owns the session. A session
nobody owns keeps opening in-process; the user's background daemon must not
turn every launch into a reduced-feature remote client (see the 4a spec's
first decision).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..session.claim import Holder, read_holder
from .runtime import read_runtime

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 1.0

# ``fetch(url, token) -> parsed JSON body | None``. Injected by the launch
# tests; the default speaks HTTP through urllib.
Fetch = Callable[[str, "str | None"], "dict | None"]


@dataclass(frozen=True)
class RemoteTarget:
    """Everything a remote link needs to open: where the daemon is, how to
    authenticate, and which session to drive."""

    endpoint: str  # base URL, e.g. "http://127.0.0.1:8642"
    token: str
    workspace_id: str
    session_id: str
    # The workspace on disk. Known at discovery; the remote read model
    # reports it (header, `!`-less shell context) without a round trip.
    workspace_root: Path

    @property
    def session_url(self) -> str:
        return f"{self.endpoint}/v1/workspaces/{self.workspace_id}/sessions/{self.session_id}"


@dataclass(frozen=True)
class AttachDecision:
    """The outcome of :func:`discover`: a target to attach to, or the reason
    no attach was possible. ``reason`` is None when the claim is not a
    daemon's at all — the caller then prints today's refusal unchanged."""

    target: RemoteTarget | None = None
    reason: str | None = None


def urllib_fetch(url: str, token: str | None) -> dict | None:
    """GET ``url`` (bearer-authenticated when ``token`` is given) and parse the
    JSON body; None on any failure. The one network call this module makes."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:  # noqa: S310
            body = resp.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def read_token(state_dir: Path) -> str | None:
    """The daemon's bearer token, read (never created) from its state dir."""
    try:
        token = (state_dir / "token").read_text().strip()
    except OSError:
        return None
    return token or None


def _workspace_id(workspaces: dict, workspace: Path) -> str | None:
    wanted = workspace.resolve()
    for record in workspaces.get("workspaces", []):
        try:
            if Path(record["path"]).resolve() == wanted:
                return str(record["id"])
        except (KeyError, TypeError, OSError):
            continue
    return None


def _probe(holder: Holder, state_dir: Path, fetch: Fetch) -> str | None:
    """Reachability + identity checks on the daemon the claim names. Returns
    the reason it cannot be attached to, or None when it can."""
    endpoint = (holder.endpoint or "").rstrip("/")
    if not endpoint:
        return "its claim names no endpoint"
    if fetch(f"{endpoint}/v1/health", None) is None:
        return f"the daemon at {endpoint} is not answering"
    runtime = read_runtime(state_dir)
    if runtime is not None and runtime.pid != holder.pid:
        return (
            f"the claim was written by pid {holder.pid} but the daemon in "
            f"{state_dir} is pid {runtime.pid}"
        )
    return None


def discover(
    workspace: Path,
    session_id: str,
    session_path: Path,
    *,
    state_dir: Path,
    fetch: Fetch = urllib_fetch,
) -> AttachDecision:
    """Decide whether the process holding ``session_path``'s claim is a daemon
    this launch can attach to.

    Only meaningful right after ``try_acquire`` returned None (the sidecar
    outlives the lock — see ``read_holder``). ``reason`` is filled in only for
    a daemon claim that failed a check, so the launch can say why the attach
    was skipped before printing the usual refusal."""
    holder = read_holder(session_path)
    if holder is None or holder.kind != "daemon":
        return AttachDecision()
    why = _probe(holder, state_dir, fetch)
    if why is not None:
        return AttachDecision(reason=why)
    endpoint = (holder.endpoint or "").rstrip("/")
    token = read_token(state_dir)
    if token is None:
        return AttachDecision(reason=f"no readable token at {state_dir / 'token'}")
    workspaces = fetch(f"{endpoint}/v1/workspaces", token)
    if workspaces is None:
        return AttachDecision(reason="the daemon refused the token (GET /v1/workspaces failed)")
    workspace_id = _workspace_id(workspaces, workspace)
    if workspace_id is None:
        return AttachDecision(reason=f"{workspace} is not a registered workspace on the daemon")
    return AttachDecision(
        target=RemoteTarget(
            endpoint=endpoint,
            token=token,
            workspace_id=workspace_id,
            session_id=session_id,
            workspace_root=workspace,
        )
    )
