"""codex/approvals.py: Mode -> Codex policy/sandbox, and the server-request broker."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai import ToolApproved, ToolDenied

from marim_harness.codex.approvals import (
    ApprovalBroker,
    Decision,
    UiSeams,
    decide,
    policy_for,
    sandbox_for,
    sandbox_mode_for,
)
from marim_harness.codex.rpc import RpcError
from marim_harness.runtime.permissions import Mode

pytestmark = pytest.mark.anyio


def test_policy_for_maps_modes():
    assert policy_for(Mode.auto) == "on-request"
    assert policy_for(Mode.ask) == "untrusted"
    assert policy_for(Mode.plan) == "never"


def test_sandbox_for_plan_is_read_only_and_others_are_workspace_write():
    assert sandbox_for(Mode.plan, "/w") == {"type": "readOnly", "networkAccess": False}
    ws = sandbox_for(Mode.auto, "/w")
    assert ws["type"] == "workspaceWrite" and ws["writableRoots"] == ["/w"]
    assert ws["networkAccess"] is True
    # read_only forces the read-only sandbox regardless of mode (read-only spawns).
    assert sandbox_for(Mode.auto, "/w", read_only=True)["type"] == "readOnly"
    assert sandbox_mode_for(Mode.ask) == "workspace-write"
    assert sandbox_mode_for(Mode.plan) == "read-only"
    assert sandbox_mode_for(Mode.auto, read_only=True) == "read-only"


def _cmd(cmd: str = "ls") -> dict:
    return {"itemId": "c1", "command": cmd, "cwd": "/w", "reason": None}


def _change(path: str) -> dict:
    return {"itemId": "f1", "changes": [{"path": path, "kind": {"type": "update"}, "diff": "+x"}]}


def test_decide_plan_declines_everything():
    d = decide(Mode.plan, "item/commandExecution/requestApproval", _cmd(), Path("/w"), None)
    assert d == Decision(accept=False, reason="plan mode: read-only")


def test_decide_auto_accepts_in_root_and_asks_outside(tmp_path):
    inside = _change(str(tmp_path / "a.py"))
    outside = _change("/etc/passwd")
    assert decide(Mode.auto, "item/fileChange/requestApproval", inside, tmp_path, None).accept
    d = decide(Mode.auto, "item/fileChange/requestApproval", outside, tmp_path, None)
    assert d.ask is True and not d.accept
    assert decide(Mode.auto, "item/commandExecution/requestApproval", _cmd(), tmp_path, None).accept


def test_decide_ask_auto_accepts_scratchpad_writes_only(tmp_path):
    pad = tmp_path / "pad"
    pad.mkdir()
    in_pad = _change(str(pad / "notes.md"))
    in_root = _change(str(tmp_path / "a.py"))
    assert decide(Mode.ask, "item/fileChange/requestApproval", in_pad, tmp_path, pad).accept
    d = decide(Mode.ask, "item/fileChange/requestApproval", in_root, tmp_path, pad)
    assert d.ask and not d.accept
    assert decide(Mode.ask, "item/commandExecution/requestApproval", _cmd(), tmp_path, pad).ask


def test_decide_auto_rejects_dotdot_traversal_and_symlink_escape(tmp_path):
    # A ".."-relative path that naively looks like a workspace child but
    # resolves outside the root once normalized.
    traversal = _change(str(tmp_path / ".." / f"{tmp_path.name}-sibling" / "secret.txt"))
    d = decide(Mode.auto, "item/fileChange/requestApproval", traversal, tmp_path, None)
    assert d.ask is True and not d.accept

    # A symlink physically inside the root pointing at a directory outside it.
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "escape"
    link.symlink_to(outside)
    via_symlink = _change(str(link / "secret.txt"))
    d2 = decide(Mode.auto, "item/fileChange/requestApproval", via_symlink, tmp_path, None)
    assert d2.ask is True and not d2.accept


def _broker(mode: Mode, tmp_path: Path, *, request_approval=None, ask_user=None, label=""):
    return ApprovalBroker(
        mode_getter=lambda: mode,
        workspace_root=tmp_path,
        scratchpad_getter=lambda: None,
        ui=UiSeams(request_approval=request_approval, ask_user=ask_user),
        label=label,
    )


async def test_handle_plan_declines_without_prompting(tmp_path):
    called = []

    async def approver(call):
        called.append(call)
        return True

    broker = _broker(Mode.plan, tmp_path, request_approval=approver)
    reply = await broker.handle("item/commandExecution/requestApproval", _cmd())
    assert reply == {"decision": "decline"} and called == []


async def test_handle_ask_routes_to_request_approval_as_tool_call_part(tmp_path):
    seen = []

    async def approver(call):
        seen.append(call)
        return True

    broker = _broker(Mode.ask, tmp_path, request_approval=approver, label="worker")
    reply = await broker.handle("item/commandExecution/requestApproval", _cmd("rm -rf build"))
    assert reply == {"decision": "accept"}
    call = seen[0]
    assert call.tool_name == "bash" and call.tool_call_id == "c1"
    assert call.args == {"command": "rm -rf build", "cwd": "/w", "label": "worker"}


async def test_handle_ask_file_change_prompt_carries_path_in_args(tmp_path):
    # Escalated outside the scratchpad -> prompts. The ApprovalPanel must not
    # render this blind: args_for's fileChange branch (translate.py) has to
    # surface the path being changed.
    seen = []

    async def approver(call):
        seen.append(call)
        return True

    outside = _change("/etc/hosts")
    broker = _broker(Mode.ask, tmp_path, request_approval=approver)
    reply = await broker.handle("item/fileChange/requestApproval", outside)
    assert reply == {"decision": "accept"}
    call = seen[0]
    assert call.tool_name == "apply_patch"
    assert call.args["path"] == "/etc/hosts"
    assert call.args


async def test_handle_ask_accepts_when_request_approval_returns_tool_approved(tmp_path):
    async def approver(call):
        return ToolApproved()

    broker = _broker(Mode.ask, tmp_path, request_approval=approver)
    reply = await broker.handle("item/commandExecution/requestApproval", _cmd())
    assert reply == {"decision": "accept"}


async def test_handle_ask_denied_and_tool_denied_both_decline(tmp_path):
    async def deny_false(call):
        return False

    async def deny_obj(call):
        return ToolDenied("nope")

    assert (
        await _broker(Mode.ask, tmp_path, request_approval=deny_false).handle(
            "item/commandExecution/requestApproval", _cmd()
        )
    ) == {"decision": "decline"}
    assert (
        await _broker(Mode.ask, tmp_path, request_approval=deny_obj).handle(
            "item/commandExecution/requestApproval", _cmd()
        )
    ) == {"decision": "decline"}


async def test_handle_ask_without_approver_declines(tmp_path):
    broker = _broker(Mode.ask, tmp_path)  # headless: no approver bound
    reply = await broker.handle("item/fileChange/requestApproval", _change(str(tmp_path / "a")))
    assert reply == {"decision": "decline"}


async def test_handle_auto_accepts_without_prompting(tmp_path):
    broker = _broker(Mode.auto, tmp_path)
    assert (await broker.handle("item/commandExecution/requestApproval", _cmd())) == {
        "decision": "accept"
    }


async def test_handle_cancelled_prompt_answers_cancel(tmp_path):
    async def approver(call):
        raise asyncio.CancelledError

    broker = _broker(Mode.ask, tmp_path, request_approval=approver)
    with pytest.raises(asyncio.CancelledError):
        await broker.handle("item/commandExecution/requestApproval", _cmd())
    # last_reply still reflects the outcome so a caller/test can observe it
    # even though the CancelledError propagates past this call.
    assert broker.last_reply == {"decision": "cancel"}


async def test_handle_permissions_request_echoes_on_accept(tmp_path):
    params = {"itemId": "p1", "permissions": {"network": True}, "reason": "curl"}
    ok = await _broker(Mode.auto, tmp_path).handle("item/permissions/requestApproval", params)
    assert ok == {"decision": "accept", "permissions": {"network": True}}
    no = await _broker(Mode.plan, tmp_path).handle("item/permissions/requestApproval", params)
    assert no == {"decision": "decline", "permissions": {}}


async def test_handle_user_input_uses_ask_user(tmp_path):
    async def ask(questions):
        assert [q.question for q in questions] == ["Which db?"]
        assert [c.label for c in questions[0].options] == ["sqlite", "postgres"]
        return {questions[0].header: "postgres"}

    params = {
        "itemId": "u1",
        "questions": [
            {
                "id": "db",
                "header": "Database",
                "question": "Which db?",
                "options": [{"label": "sqlite"}, {"label": "postgres", "description": "prod"}],
            },
        ],
    }
    reply = await _broker(Mode.auto, tmp_path, ask_user=ask).handle(
        "item/tool/requestUserInput", params
    )
    assert reply == {"answers": {"db": {"answers": ["postgres"]}}}


async def test_handle_user_input_headless_picks_first_option(tmp_path):
    params = {
        "itemId": "u1",
        "questions": [
            {"id": "q", "question": "Pick", "options": [{"label": "a"}, {"label": "b"}]},
            {"id": "free", "question": "Anything else?", "options": []},
        ],
    }
    reply = await _broker(Mode.auto, tmp_path).handle("item/tool/requestUserInput", params)
    assert reply == {"answers": {"q": {"answers": ["a"]}, "free": {"answers": [""]}}}


async def test_handle_elicitation_declines_and_unknown_raises(tmp_path):
    broker = _broker(Mode.auto, tmp_path)
    assert (await broker.handle("mcpServer/elicitation/request", {"itemId": "e"})) == {
        "action": "decline"
    }
    with pytest.raises(RpcError) as exc:
        await broker.handle("item/somethingNew/requestApproval", {})
    assert exc.value.code == -32601


async def test_label_for_prefixes_a_childs_request_with_its_agent_name(tmp_path):
    """An adopted collab child shares the parent's broker; ``label_for``
    (set by the thread's owner from the collab router) names the agent on
    the panel entry — joined to a spawn's own label when there is one."""
    seen = []

    async def approver(call):
        seen.append(call)
        return True

    broker = _broker(Mode.ask, tmp_path, request_approval=approver, label="worker")
    broker.label_for = lambda tid: "agent scout" if tid == "c1" else None
    await broker.handle(
        "item/commandExecution/requestApproval", {**_cmd("rm -rf build"), "threadId": "c1"}
    )
    await broker.handle(
        "item/commandExecution/requestApproval", {**_cmd("rm -rf build"), "threadId": "t1"}
    )
    assert seen[0].args["label"] == "worker / agent scout"
    assert seen[1].args["label"] == "worker"
    plain = _broker(Mode.ask, tmp_path, request_approval=approver)
    plain.label_for = lambda tid: "agent scout"
    await plain.handle("item/commandExecution/requestApproval", {**_cmd("x"), "threadId": "c1"})
    assert seen[2].args["label"] == "agent scout"
