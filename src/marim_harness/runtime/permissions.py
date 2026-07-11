import json
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path

from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.tools import DeferredToolApprovalResult

from ..read_only_commands import is_read_only
from ..tools.names import NET_TOOLS
from ..workspace.fs import WorkspaceError, resolve_in_workspace


class Mode(str, Enum):
    ask = "ask"
    auto = "auto"
    plan = "plan"

    def cycle(self) -> "Mode":
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


def _is_scratchpad_write(call: object, root: Path, scratchpad: Path) -> bool:
    """True when this approval call is a write_file/edit_file whose target
    resolves inside the scratchpad. Mirrors the tool layer's own resolution
    order (_safe_write in tools/impl/fs.py): the workspace root is tried
    first, so a relative path — which always lands in the workspace — can
    never be mistaken for a scratchpad write; only a path the workspace
    guard rejects may qualify. Resolution chases symlinks and ``..``, so the
    check is on the real target, not a string prefix."""
    if getattr(call, "tool_name", None) not in ("write_file", "edit_file"):
        return False
    path = str(_call_args(call).get("path", ""))
    if not path:
        return False
    try:
        resolve_in_workspace(root, path)
        return False  # a workspace write: normal gating applies
    except WorkspaceError:
        pass
    try:
        resolve_in_workspace(scratchpad, path)
    except WorkspaceError:
        return False
    return True


def _plan_decision(call: object) -> "bool | ToolDenied":
    """Plan mode is read-only. Deny mutations, but let a read-only ``bash``
    command through so the agent can research before presenting a plan
    (see read_only_commands.is_read_only — best-effort, not a sandbox)."""
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
    results = DeferredToolResults()
    for call in requests.approvals:
        if mode is Mode.auto:
            results.approvals[call.tool_call_id] = True
        elif mode is Mode.plan:
            results.approvals[call.tool_call_id] = _plan_decision(call)
        elif (
            scratchpad is not None
            and workspace_root is not None
            and _is_scratchpad_write(call, workspace_root, scratchpad)
        ):
            # Scratchpad writes are pre-blessed in ask mode — the directory
            # exists precisely so intermediate work doesn't prompt (the
            # instructions block advertises exactly that). bash never
            # qualifies: a command's filesystem reach can't be cheaply
            # proven to stay inside the scratchpad.
            results.approvals[call.tool_call_id] = True
        elif request_approval is None:
            results.approvals[call.tool_call_id] = ToolDenied(
                "no approver available; denied"
            )
        else:  # Mode.ask
            results.approvals[call.tool_call_id] = await request_approval(call)
    return results
