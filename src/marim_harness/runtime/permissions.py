from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..read_only_commands import is_read_only
from ..tools.names import NET_TOOLS
from ..workspace.fs import WorkspaceError, resolve_in_workspace

# pydantic_ai's construction classes (ToolApproved/ToolDenied/DeferredToolResults)
# are imported lazily, inside the functions that actually build one, rather than
# at module level: this module is on the cheap CLI import path (mcp/config.py
# imports just the `Mode` enum from here for its approval hook, and that in turn
# sits behind `marim trust` -> trust_surface -> mcp.config), so a module-level
# `import pydantic_ai` would drag its ~0.5s transitive weight into a status
# command that never resolves an actual approval. Every other caller of this
# module (the turn controller, harness, sub-agent runner, ...) already pays for
# pydantic_ai elsewhere, so this costs the real callers nothing.
if TYPE_CHECKING:
    from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolApproved, ToolDenied
    from pydantic_ai.tools import DeferredToolApprovalResult


class Mode(str, Enum):
    ask = "ask"
    auto = "auto"
    plan = "plan"

    def cycle(self) -> Mode:
        order = [Mode.ask, Mode.auto, Mode.plan]
        return order[(order.index(self) + 1) % len(order)]


def _call_args(call: object) -> dict:
    """Best-effort tool args from an approval call, as a dict. Args arrive as
    a dict or, from some providers, a JSON string."""
    args = getattr(call, "args", None)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return {}
    return args if isinstance(args, dict) else {}


def _bash_command(call: object) -> str:
    """Best-effort extract the ``command`` arg from an approval call."""
    return str(_call_args(call).get("command", ""))


def _scratchpad_write_target(call: object, root: Path, scratchpad: Path) -> Path | None:
    """The resolved canonical target of a write_file/edit_file call when it lands
    inside the scratchpad, else ``None``. Mirrors the tool layer's own resolution
    order (_safe_write in tools/impl/fs.py): the workspace root is tried first, so
    a relative path — which always lands in the workspace — can never be mistaken
    for a scratchpad write; only a path the workspace guard rejects may qualify.
    Resolution chases symlinks and ``..``, so the decision — and the returned
    target — are the real file, not a string prefix."""
    if getattr(call, "tool_name", None) not in ("write_file", "edit_file"):
        return None
    path = str(_call_args(call).get("path", ""))
    if not path:
        return None
    try:
        resolve_in_workspace(root, path)
        return None  # a workspace write: normal gating applies
    except WorkspaceError:
        pass
    try:
        return resolve_in_workspace(scratchpad, path)
    except WorkspaceError:
        return None


def _scratchpad_approval(
    call: object, workspace_root: Path | None, scratchpad: Path | None
) -> ToolApproved | None:
    """The ask-mode scratchpad auto-approval, or ``None`` when the call isn't a
    scratchpad write (so the caller falls through to normal gating).

    Crucially, the returned ``ToolApproved`` PINS the resolved canonical target
    via ``override_args`` instead of a bare ``True``. Between this approval and
    the tool actually opening the file there is a TOCTOU window: the original
    ``path`` may name a symlink under the scratchpad, and an attacker who can
    write there could repoint it after we bless it, redirecting the write out of
    the scratchpad. Handing the executor the already-resolved absolute path (not
    the symlink name) closes that specific swap — the tool writes the exact file
    we approved, not whatever the name later points to. (Residual: fully defeating
    a swap of a *parent component* of the resolved path needs an ``O_NOFOLLOW``
    open in the tool executor — tools/impl/fs.py — which this layer can't reach.)
    """
    if workspace_root is None or scratchpad is None:
        return None
    target = _scratchpad_write_target(call, workspace_root, scratchpad)
    if target is None:
        return None
    args = dict(_call_args(call))
    args["path"] = str(target)
    from pydantic_ai import ToolApproved  # lazy — see module-level note above.

    return ToolApproved(override_args=args)


def _plan_decision(call: object) -> bool | ToolDenied:
    """Plan mode is read-only. Deny mutations, but let a read-only ``bash``
    command through so the agent can research before presenting a plan
    (see read_only_commands.is_read_only — best-effort, not a sandbox)."""
    from pydantic_ai import ToolDenied  # lazy — see module-level note above.

    tool_name = getattr(call, "tool_name", None)
    if tool_name == "bash" and is_read_only(_bash_command(call)):
        return True
    if tool_name == "bash":
        return ToolDenied("plan mode: read-only commands only")
    # Outbound network tools (fetch_url/web_search) are gated like mutations, so
    # in plan mode they land here rather than being auto-allowed. Plan mode is
    # presented to the user as read-only *local* research; a prompt-injected agent
    # could otherwise read any host file and exfiltrate it through a fetch URL or
    # search query with zero approval. Deny with wording that names egress, since
    # a bare "read-only" reads to a model as "fetching is fine" — switch to ask/
    # auto mode to actually reach the network.
    if tool_name in NET_TOOLS:
        return ToolDenied(
            "plan mode is local-research only; outbound network "
            "(fetch_url/web_search) is disabled here — switch out of plan mode to fetch"
        )
    return ToolDenied("read-only plan mode")


async def resolve_approvals(
    requests: DeferredToolRequests,
    mode: Mode,
    request_approval: Callable[[object], Awaitable[DeferredToolApprovalResult | bool]] | None,
    *,
    workspace_root: Path | None = None,
    scratchpad: Path | None = None,
) -> DeferredToolResults:
    """Turn pending tool-approval requests into results based on the current mode.

    auto -> approve all. plan -> deny mutations; read-only bash is approved
    (see _plan_decision). ask -> auto-approve write_file/edit_file targeting
    the scratchpad (when one is wired), otherwise delegate to callback, which
    returns True (approve) or a ToolDenied (reject). In ask mode with no
    callback wired (e.g. a non-interactive run), deny rather than crash — nothing
    can grant approval, so the safe answer is to refuse.
    """
    from pydantic_ai import DeferredToolResults, ToolDenied  # lazy — see module note above.

    results = DeferredToolResults()
    for call in requests.approvals:
        # Scratchpad writes are pre-blessed in ask mode — the directory exists
        # precisely so intermediate work doesn't prompt (the instructions block
        # advertises exactly that). bash never qualifies: a command's filesystem
        # reach can't be cheaply proven to stay inside the scratchpad. The
        # approval pins the resolved path (see _scratchpad_approval) rather than a
        # bare True. Computed once per call so the elif chain reads a value.
        scratch_ok = (
            _scratchpad_approval(call, workspace_root, scratchpad)
            if mode is Mode.ask
            else None
        )
        if mode is Mode.auto:
            results.approvals[call.tool_call_id] = True
        elif mode is Mode.plan:
            results.approvals[call.tool_call_id] = _plan_decision(call)
        elif scratch_ok is not None:
            # ask mode + a scratchpad write: the resolved-path-pinned approval.
            results.approvals[call.tool_call_id] = scratch_ok
        elif request_approval is None:
            results.approvals[call.tool_call_id] = ToolDenied(
                "no approver available; denied"
            )
        else:  # Mode.ask
            results.approvals[call.tool_call_id] = await request_approval(call)
    return results
