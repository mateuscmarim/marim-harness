"""``can_use_tool`` → marim's Mode, approval panel, or ask_user
(spec §Mode → approval mapping).

The CLI runs in its ``default`` permission mode and asks marim, over stdio,
before every tool it would not auto-allow. ``classify`` turns the request
into the transport-neutral ``ExternalRequest`` the shared policy core
(``runtime/permissions.decide_external``) decides on; the broker then
answers directly, prompts through ``UiSeams.request_approval`` (the
ApprovalPanel in the TUI, None headless), or routes ``AskUserQuestion``
through ``UiSeams.ask_user``. Deny messages are written for Claude — they
land verbatim in its tool_result — so each says what was refused and what
to do instead.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..ask_user import Choice, Question
from ..runtime.permissions import (
    PLAN_READ_ONLY,
    Decision,
    ExternalRequest,
    Mode,
    Reach,
    UiSeams,
    decide_external,
)

logger = logging.getLogger(__name__)

QUESTION_TOOL = "AskUserQuestion"
READ_ONLY_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "LS",
        "TodoRead",
        "TaskGet",
        "TaskList",
        "ListAgents",
        "ToolSearch",
    }
)
# Read-only locally, but outbound egress — a third class, not a subset of the
# set above: plan mode has to refuse these while ask/auto let them through
# ungated (marim's own fetch_url/web_search are ungated there too). Kept apart
# from READ_ONLY_TOOLS so a tool can never be added to the wrong one by
# accident.
NETWORK_TOOLS = frozenset({"WebFetch", "WebSearch"})
_PATH_KEYS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

PLAN_DENY_MESSAGE = "plan mode: read-only — describe the change instead of making it"
# A refused WebFetch/WebSearch is the same PLAN_READ_ONLY verdict but a
# different refusal: nothing was being *changed*, so "describe the change
# instead" is advice Claude cannot act on. Deny messages land verbatim in the
# tool_result, so the egress case gets its own wording — say what you wanted
# to look up, and the user decides whether to leave plan mode for it.
PLAN_EGRESS_MESSAGE = (
    "plan mode: no outbound requests — say what you would look up and why; the user "
    "switches out of plan mode with /mode if they want it fetched"
)
# Claude's own plan mode (the process runs in it whenever marim is in plan
# mode, see ``controls.py``) ends with an ``ExitPlanMode`` call asking the
# user to approve the plan and switch modes. marim owns the mode, so the
# call is refused with a hint that keeps Claude's plan-mode workflow
# useful: present the plan, and the user switches with ``/mode``.
EXIT_PLAN_MESSAGE = (
    "plan mode is set by the user in marim, not by this tool: present the plan "
    "as your answer; the user switches out of plan mode with /mode when they "
    "want it carried out"
)
CANCELLED_MESSAGE = "cancelled by user"
USER_DENIED_MESSAGE = "denied by the user; do not retry this action, ask what they want instead"
HEADLESS_DENY_MESSAGE = (
    "no approver is attached (headless run); this action is not permitted here — "
    "explain what you would have done instead"
)
NO_USER_MESSAGE = (
    "the user is not available to answer; proceed on your best judgement and say what you assumed"
)


@dataclass(frozen=True)
class ToolRequest:
    mutating: bool
    paths: tuple[Path, ...] = ()
    question: bool = False
    network: bool = False


def classify(tool_name: str, tool_input: dict, annotations: dict | None = None) -> ToolRequest:
    """The spec's tool table. Unknown tools are mutating (the safe default);
    an MCP tool is read-only only when the CLI's annotations say so."""
    if tool_name == QUESTION_TOOL:
        return ToolRequest(mutating=False, question=True)
    if tool_name in NETWORK_TOOLS:
        return ToolRequest(mutating=False, network=True)
    if tool_name in READ_ONLY_TOOLS:
        return ToolRequest(mutating=False)
    if tool_name.startswith("mcp__"):
        # Trusting the server's own ``readOnlyHint`` is inside the trust
        # boundary: an MCP server only reaches a marim session at all once the
        # project (or plugin) that declares it has been trusted, and a trusted
        # server is already running arbitrary code of its own. See mcp/ and
        # docs/guides/trust.md.
        return ToolRequest(mutating=not bool((annotations or {}).get("readOnlyHint")))
    key = _PATH_KEYS.get(tool_name)
    if key is not None:
        raw = tool_input.get(key)
        return ToolRequest(mutating=True, paths=(Path(str(raw)),) if raw else ())
    return ToolRequest(mutating=True)


def allow_reply(tool_input: dict) -> dict:
    return {"behavior": "allow", "updatedInput": dict(tool_input)}


def deny_reply(message: str) -> dict:
    return {"behavior": "deny", "message": message}


def wire_deny_message(decision: Decision, tool_name: str = "") -> str:
    """The shared core's reason is a log label; on the wire plan mode gets
    the fuller hint Claude can act on — the one that names who owns the mode
    for ``ExitPlanMode``, and the egress wording for a refused web tool, which
    is read-only and so has no "change" to describe instead."""
    if decision.reason == PLAN_READ_ONLY:
        if tool_name == "ExitPlanMode":
            return EXIT_PLAN_MESSAGE
        return PLAN_EGRESS_MESSAGE if tool_name in NETWORK_TOOLS else PLAN_DENY_MESSAGE
    return decision.reason or "not permitted"


def _is_approved(result: object) -> bool:
    """Same acceptance rule as native gating: ``True`` or a ToolApproved is a
    yes; ``False``/``None``/ToolDenied is a no."""
    if result is True:
        return True
    if not result:
        return False
    from pydantic_ai import ToolApproved  # lazy — see module note in runtime/permissions.py.

    return isinstance(result, ToolApproved)


def _denial_message(result: object) -> str:
    message = getattr(result, "message", None)
    return str(message) if message else USER_DENIED_MESSAGE


def _question(raw: dict) -> Question:
    text = str(raw.get("question", ""))
    return Question(
        question=text,
        header=str(raw.get("header") or text),
        options=[
            Choice(label=str(o.get("label", "")), description=o.get("description"))
            for o in raw.get("options") or []
            if o.get("label")
        ],
        multi=bool(raw.get("multiSelect")),
    )


def _answer_text(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value or "")


class ClaudeApprovalBroker:
    def __init__(
        self,
        *,
        mode_getter: Callable[[], Mode],
        reach: Reach,
        ui: UiSeams,
        label: str = "",
    ) -> None:
        self._mode_getter = mode_getter
        # Only the mode is read through a getter per request: the workspace
        # root and the --unsafe-full-access flag inside ``reach`` cannot change
        # while the session runs (see WorkspaceConfig.full_access), and the
        # scratchpad carries its own getter.
        self._reach = reach
        self._root = reach.root
        self._ui = ui
        self._label = label
        self._lock = asyncio.Lock()
        self.last_reply: dict | None = None

    async def handle(self, request_id: str, request: dict) -> dict:
        """Answer one ``control_request``. Serialized: the CLI can raise two
        ``can_use_tool`` requests from parallel tool calls and the panel shows
        one at a time. A cancelled handler (the client's
        ``control_cancel_request`` path, or ``aclose``) records the cancel and
        re-raises so nothing is written for a request the CLI abandoned."""
        async with self._lock:
            try:
                reply = await self._handle(request)
            except asyncio.CancelledError:
                self.last_reply = deny_reply(CANCELLED_MESSAGE)
                raise
            self.last_reply = reply
            return reply

    async def _handle(self, request: dict) -> dict:
        subtype = request.get("subtype")
        if subtype != "can_use_tool":
            raise ValueError(f"unsupported control_request {subtype!r}")
        tool_name = str(request.get("tool_name") or "")
        tool_input = dict(request.get("input") or {})
        req = classify(tool_name, tool_input, request.get("annotations"))
        if req.question or request.get("requires_user_interaction"):
            return await self._ask(tool_input)
        decision = decide_external(
            self._mode_getter(),
            self._anchored(req),
            self._root,
            self._reach.scratchpad(),
            full_access=self._reach.full_access,
        )
        if decision.ask:
            return await self._prompt(tool_name, tool_input, request, decision)
        if decision.accept:
            return allow_reply(tool_input)
        logger.info("claude %s denied (%s)", tool_name, decision.reason)
        return deny_reply(wire_deny_message(decision, tool_name))

    def _anchored(self, req: ToolRequest) -> ExternalRequest:
        """The CLI runs with cwd = the workspace root, so a relative
        ``file_path`` means "under the root" — resolve it there before the
        policy check rather than against marim's own cwd."""
        root = self._root or Path(".")
        paths = tuple(p if p.is_absolute() else root / p for p in req.paths)
        return ExternalRequest(mutating=req.mutating, paths=paths, network=req.network)

    async def _prompt(
        self, tool_name: str, tool_input: dict, request: dict, decision: Decision
    ) -> dict:
        """Route to ``request_approval`` as a pydantic-ai ToolCallPart so the
        ApprovalPanel renders it like a native gated call. Headless (no
        approver) → denied, exactly like ``resolve_approvals``."""
        if self._ui.request_approval is None:
            return deny_reply(HEADLESS_DENY_MESSAGE)
        from pydantic_ai.messages import ToolCallPart

        from ..subagents.cli_backend import (
            normalize_cc_tool,  # lazy: cli_backend imports claude.process
        )

        name, args = normalize_cc_tool(tool_name, tool_input)
        args = dict(args)
        if decision.reason:
            args["reason"] = decision.reason
        if self._label:
            args["label"] = self._label
        call = ToolCallPart(
            tool_name=name, args=args, tool_call_id=str(request.get("tool_use_id") or "")
        )
        result = await self._ui.request_approval(call)
        if _is_approved(result):
            return allow_reply(tool_input)
        logger.info("claude %s denied by the user", tool_name)
        return deny_reply(_denial_message(result))

    async def _ask(self, tool_input: dict) -> dict:
        questions = [_question(q) for q in tool_input.get("questions") or []]
        answers: dict | None = None
        if self._ui.ask_user is not None and questions:
            answers = await self._ui.ask_user(questions)
        if not answers:
            return deny_reply(NO_USER_MESSAGE)
        updated = dict(tool_input)
        updated["answers"] = {q.question: _answer_text(answers.get(q.header)) for q in questions}
        return allow_reply(updated)
