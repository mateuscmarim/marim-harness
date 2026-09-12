"""Mode -> Codex approval policy/sandbox, and the server-request broker.

Codex asks *marim* for approvals (``item/*/requestApproval``), for user input
(``item/tool/requestUserInput``) and for MCP elicitation answers over the
JSON-RPC back-channel. This module turns those into the same UI seams the
native tools use — ``Deps.request_approval`` (the ApprovalPanel in the TUI,
None headless) and ``Deps.ask_user`` — so a Codex turn in ask mode gates
exactly like a native turn, and plan mode never prompts.

The mapping (spec §Mode mapping):

    marim mode   approvalPolicy   sandbox (thread)   sandboxPolicy (turn)
    auto         on-request       workspace-write    workspaceWrite[root]
    ask          untrusted        workspace-write    workspaceWrite[root]
    plan         never            read-only          readOnly

``acceptForSession`` is never sent: marim keeps per-call gating so the user's
/mode switch mid-session takes effect on the very next request.

``decide`` is a pure function (mode + request -> Decision) so the policy is
unit-testable without an event loop; ``ApprovalBroker.handle`` is the thin
async wrapper that owns the callbacks and serializes prompts with a lock
(Codex can raise two approvals concurrently from parallel tool calls; the
ApprovalPanel shows one at a time).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from ..ask_user import Choice, Question
from ..runtime.permissions import Decision, ExternalRequest, Mode, UiSeams, decide_external
from .rpc import RpcError
from .translate import args_for, tool_name_for

__all__ = [
    "ApprovalBroker",
    "Decision",
    "UiSeams",
    "decide",
    "policy_for",
    "sandbox_for",
    "sandbox_mode_for",
]

logger = logging.getLogger(__name__)

METHOD_NOT_FOUND = -32601

_APPROVAL_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
    }
)
USER_INPUT_METHOD = "item/tool/requestUserInput"
ELICITATION_METHOD = "mcpServer/elicitation/request"


def policy_for(mode: Mode) -> str:
    """``approvalPolicy`` for a marim mode."""
    if mode is Mode.auto:
        return "on-request"
    if mode is Mode.ask:
        return "untrusted"
    return "never"


def sandbox_mode_for(mode: Mode, *, read_only: bool = False) -> str:
    """``thread/start.sandbox`` for a marim mode (plan, or a read-only spawn, is
    read-only; everything else is workspace-write)."""
    return "read-only" if (read_only or mode is Mode.plan) else "workspace-write"


def sandbox_for(mode: Mode, root: str, *, read_only: bool = False) -> dict:
    """``turn/start.sandboxPolicy`` for a marim mode. Network stays on for
    workspace-write (marim's own net tools are ungated in auto/ask; plan mode
    is local-research only, so the read-only sandbox has it off)."""
    if read_only or mode is Mode.plan:
        return {"type": "readOnly", "networkAccess": False}
    return {
        "type": "workspaceWrite",
        "writableRoots": [root],
        "networkAccess": True,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


def _change_paths(params: dict) -> list[Path]:
    return [Path(str(c.get("path", ""))) for c in params.get("changes") or [] if c.get("path")]


def decide(
    mode: Mode,
    method: str,
    params: dict,
    workspace_root: Path | None,
    scratchpad: Path | None,
) -> Decision:
    """Codex's approval requests, shaped for the shared table: every
    ``requestApproval`` is a mutation (Codex never asks for reads), and a
    fileChange names its paths so auto/ask can apply the workspace and
    scratchpad rules. See ``runtime.permissions.decide_external`` for the
    per-mode table."""
    paths = _change_paths(params) if method == "item/fileChange/requestApproval" else []
    req = ExternalRequest(mutating=True, paths=tuple(paths))
    return decide_external(mode, req, workspace_root, scratchpad)


_METHOD_ITEM_TYPES = {
    "item/commandExecution/requestApproval": "commandExecution",
    "item/fileChange/requestApproval": "fileChange",
    "item/permissions/requestApproval": "permissions",
}


def _as_item(method: str, params: dict) -> dict:
    """Shape a request's params like a ThreadItem so ``tool_name_for``/
    ``args_for`` (Task 5) can name and describe it for the ApprovalPanel."""
    item = dict(params)
    item["type"] = _METHOD_ITEM_TYPES.get(method, "unknown")
    item["id"] = str(params.get("itemId", ""))
    return item


class ApprovalBroker:
    """Answers Codex server requests for one thread. ``label`` prefixes the
    ApprovalPanel entry for a spawn (``worker: bash``) so the user can tell a
    sub-agent's request from the main loop's."""

    def __init__(
        self,
        *,
        mode_getter: Callable[[], Mode],
        workspace_root: Path | None,
        scratchpad_getter: Callable[[], Path | None],
        ui: UiSeams,
        label: str = "",
    ) -> None:
        self._mode_getter = mode_getter
        self._root = workspace_root
        self._scratchpad_getter = scratchpad_getter
        self._request_approval = ui.request_approval
        self._ask_user = ui.ask_user
        self._label = label
        self._lock = asyncio.Lock()
        # The last reply this broker produced, including the "cancel" placeholder
        # set on the way out of a CancelledError. Recorded for callers/tests that
        # want to observe the outcome of a cancelled prompt — nothing in this
        # module sends it anywhere on its own; a future dispatcher wanting to
        # answer Codex on cancel would read it here.
        self.last_reply: dict | None = None

    async def handle(self, method: str, params: dict) -> dict:
        async with self._lock:
            try:
                reply = await self._handle(method, params)
            except asyncio.CancelledError:
                self.last_reply = {"decision": "cancel"}
                raise
            self.last_reply = reply
            return reply

    async def _handle(self, method: str, params: dict) -> dict:
        if method in _APPROVAL_METHODS:
            return await self._approval(method, params)
        if method == USER_INPUT_METHOD:
            return await self._user_input(params)
        if method == ELICITATION_METHOD:
            return {"action": "decline"}
        raise RpcError(METHOD_NOT_FOUND, f"unsupported server request {method}")

    async def _approval(self, method: str, params: dict) -> dict:
        mode = self._mode_getter()
        decision = decide(mode, method, params, self._root, self._scratchpad_getter())
        accept = decision.accept
        if decision.ask:
            accept = await self._prompt(method, params)
        if not accept and decision.reason:
            logger.info("codex %s declined (%s)", method, decision.reason)
        reply: dict = {"decision": "accept" if accept else "decline"}
        if method == "item/permissions/requestApproval":
            reply["permissions"] = params.get("permissions") or {} if accept else {}
        return reply

    async def _prompt(self, method: str, params: dict) -> bool:
        """Route to ``request_approval`` as a pydantic-ai ToolCallPart so the
        ApprovalPanel renders it like a native gated call. Headless (no
        approver) -> denied, exactly like ``resolve_approvals``."""
        if self._request_approval is None:
            return False
        from pydantic_ai.messages import ToolCallPart

        item = _as_item(method, params)
        args = args_for(item)
        if params.get("reason"):
            args.setdefault("reason", str(params["reason"]))
        if self._label:
            args["label"] = self._label
        call = ToolCallPart(
            tool_name=tool_name_for(item) or "codex",
            args=args,
            tool_call_id=str(params.get("itemId", "")),
        )
        result = await self._request_approval(call)
        return _is_approved(result)

    async def _user_input(self, params: dict) -> dict:
        questions = [_question(q) for q in params.get("questions") or []]
        ids = [str(q.get("id", "")) for q in params.get("questions") or []]
        answers: dict | None = None
        if self._ask_user is not None and questions:
            answers = await self._ask_user(questions)
        if not answers:
            # Headless / cancelled: the first option (or blank) per question,
            # logged so the choice is visible in the transcript.
            logger.info("codex requestUserInput answered with defaults (no UI)")
            answers = {q.header: (q.options[0].label if q.options else "") for q in questions}
        out = {}
        for qid, q in zip(ids, questions, strict=True):
            value = answers.get(q.header, "")
            out[qid] = {"answers": list(value) if isinstance(value, list) else [str(value)]}
        return {"answers": out}


def _is_approved(result: object) -> bool:
    """Same acceptance rule as native gating: ``True`` or a ToolApproved is a
    yes; ``False``/``None``/ToolDenied is a no."""
    if result is True:
        return True
    if not result:
        return False
    from pydantic_ai import ToolApproved  # lazy — see module note in runtime/permissions.py.

    return isinstance(result, ToolApproved)


def _question(raw: dict) -> Question:
    header = str(raw.get("header") or raw.get("id") or "")
    options = [
        Choice(label=str(o.get("label", "")), description=o.get("description"))
        for o in raw.get("options") or []
        if o.get("label")
    ]
    return Question(question=str(raw.get("question", "")), header=header, options=options)
