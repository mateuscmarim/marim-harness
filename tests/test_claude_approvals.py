"""ClaudeApprovalBroker: the can_use_tool table, prompting through the
approval panel seam, AskUserQuestion through ask_user, cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai import ToolDenied

from marim_harness.claude.approvals import (
    CANCELLED_MESSAGE,
    EXIT_PLAN_MESSAGE,
    HEADLESS_DENY_MESSAGE,
    NO_USER_MESSAGE,
    PLAN_DENY_MESSAGE,
    PLAN_EGRESS_MESSAGE,
    USER_DENIED_MESSAGE,
    ClaudeApprovalBroker,
    ToolRequest,
    allow_reply,
    classify,
    deny_reply,
)
from marim_harness.runtime.permissions import Mode, UiSeams

pytestmark = pytest.mark.anyio


# --- classify ---------------------------------------------------------------


def test_classify_read_only_tools():
    for name in ("Read", "Glob", "Grep", "LS", "TodoRead", "ToolSearch"):
        assert classify(name, {"file_path": "/x"}) == ToolRequest(mutating=False)


def test_classify_network_tools_are_read_only_but_flagged():
    for name in ("WebFetch", "WebSearch"):
        assert classify(name, {"url": "https://x"}) == ToolRequest(mutating=False, network=True)


def test_classify_file_mutators_carry_their_path():
    assert classify("Write", {"file_path": "/ws/a.py"}) == ToolRequest(
        mutating=True, paths=(Path("/ws/a.py"),)
    )
    assert classify("Edit", {"file_path": "rel.py"}) == ToolRequest(
        mutating=True, paths=(Path("rel.py"),)
    )
    assert classify("MultiEdit", {"file_path": "/m"}).paths == (Path("/m"),)
    assert classify("NotebookEdit", {"notebook_path": "/n.ipynb"}).paths == (Path("/n.ipynb"),)
    assert classify("Write", {}) == ToolRequest(mutating=True)  # no path → mutating, unanchored


def test_classify_commands_agents_mcp_and_unknown_are_mutating():
    for name in (
        "Bash",
        "Task",
        "Agent",
        "Skill",
        "CronCreate",
        "SendMessage",
        "EnterWorktree",
        "Frobnicate",
    ):
        assert classify(name, {}) == ToolRequest(mutating=True)
    assert classify("mcp__srv__tool", {}) == ToolRequest(mutating=True)
    assert classify("mcp__srv__tool", {}, {"readOnlyHint": True}) == ToolRequest(mutating=False)


def test_classify_ask_user_question_is_a_question():
    assert classify("AskUserQuestion", {"questions": []}) == ToolRequest(
        mutating=False, question=True
    )


def test_reply_shapes():
    assert allow_reply({"a": 1}) == {"behavior": "allow", "updatedInput": {"a": 1}}
    assert deny_reply("no") == {"behavior": "deny", "message": "no"}


# --- broker -----------------------------------------------------------------


class _Panel:
    """Records approval prompts; answers with the queued replies in order."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.calls: list = []
        self.release = asyncio.Event()
        self.release.set()

    async def __call__(self, call):
        self.calls.append(call)
        await self.release.wait()
        return self.replies.pop(0) if self.replies else False


def _broker(
    mode: Mode, root: Path, *, panel=None, ask_user=None, pad: Path | None = None, label=""
):
    return ClaudeApprovalBroker(
        mode_getter=lambda: mode,
        workspace_root=root,
        scratchpad_getter=lambda: pad,
        ui=UiSeams(request_approval=panel, ask_user=ask_user),
        label=label,
    )


def _write(path: str, tid: str = "tu1") -> dict:
    return {
        "subtype": "can_use_tool",
        "tool_name": "Write",
        "input": {"file_path": path, "content": "x"},
        "tool_use_id": tid,
    }


async def test_plan_denies_mutation_without_prompting(tmp_path: Path):
    panel = _Panel(True)
    broker = _broker(Mode.plan, tmp_path, panel=panel)
    reply = await broker.handle("r1", _write(str(tmp_path / "a.py")))
    assert reply == deny_reply(PLAN_DENY_MESSAGE)
    assert panel.calls == []
    assert broker.last_reply == reply


async def test_plan_tells_exit_plan_mode_who_owns_the_mode(tmp_path: Path):
    # The process runs in Claude's own plan mode (controls.py), whose
    # workflow ends in an ExitPlanMode call asking to switch modes. marim
    # owns the mode: the call is refused with the hint to present the plan
    # (not the generic "describe the change" text, which reads as if the
    # plan itself were the forbidden write).
    panel = _Panel(True)
    broker = _broker(Mode.plan, tmp_path, panel=panel)
    req = {"subtype": "can_use_tool", "tool_name": "ExitPlanMode", "input": {"plan": "1. do x"}}
    assert await broker.handle("r1", req) == deny_reply(EXIT_PLAN_MESSAGE)
    assert panel.calls == []
    # Outside plan mode the same call is an ordinary mutating tool (auto
    # accepts it, ask prompts) — Claude only makes it in plan mode anyway.
    assert await _broker(Mode.auto, tmp_path).handle("r2", req) == allow_reply(req["input"])


async def test_plan_allows_reads(tmp_path: Path):
    broker = _broker(Mode.plan, tmp_path, panel=_Panel())
    req = {
        "subtype": "can_use_tool",
        "tool_name": "Read",
        "input": {"file_path": "/etc/hosts"},
        "tool_use_id": "t",
    }
    assert await broker.handle("r1", req) == allow_reply({"file_path": "/etc/hosts"})


def _fetch(tid: str = "w1") -> dict:
    return {
        "subtype": "can_use_tool",
        "tool_name": "WebFetch",
        "input": {"url": "https://example.invalid/secrets"},
        "tool_use_id": tid,
    }


async def test_plan_denies_network_tools_without_prompting(tmp_path: Path):
    """Plan mode is local-research only: a non-mutating WebFetch/WebSearch is
    still egress, so it is refused exactly like a mutation would be — but with
    the egress wording, since there is no "change" for Claude to describe."""
    for tool in ("WebFetch", "WebSearch"):
        panel = _Panel(True)
        broker = _broker(Mode.plan, tmp_path, panel=panel)
        req = dict(_fetch(), tool_name=tool)
        assert await broker.handle("r1", req) == deny_reply(PLAN_EGRESS_MESSAGE)
        assert panel.calls == []


async def test_ask_and_auto_allow_network_tools_without_prompting(tmp_path: Path):
    for mode in (Mode.ask, Mode.auto):
        panel = _Panel(True)
        broker = _broker(mode, tmp_path, panel=panel)
        reply = await broker.handle("r1", _fetch())
        assert reply == allow_reply({"url": "https://example.invalid/secrets"})
        assert panel.calls == []


async def test_auto_allows_inside_workspace_and_prompts_outside(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    panel = _Panel(True)
    broker = _broker(Mode.auto, root, panel=panel)
    assert (await broker.handle("r1", _write(str(root / "a.py"))))["behavior"] == "allow"
    assert panel.calls == []
    stray = tmp_path / "elsewhere.py"
    reply = await broker.handle("r2", _write(str(stray), tid="tu2"))
    assert reply["behavior"] == "allow"
    call = panel.calls[0]
    assert call.tool_name == "write_file"
    assert call.args["path"] == str(stray)
    assert call.args["reason"] == f"outside workspace: {stray}"
    assert call.tool_call_id == "tu2"


async def test_ask_prompts_for_workspace_writes_and_allows_scratchpad(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    pad = tmp_path / "pad"
    pad.mkdir()
    panel = _Panel(False)
    broker = _broker(Mode.ask, root, panel=panel, pad=pad, label="worker")
    assert (await broker.handle("r1", _write(str(pad / "notes.md"))))["behavior"] == "allow"
    assert panel.calls == []
    reply = await broker.handle("r2", _write(str(root / "a.py")))
    assert reply == deny_reply(USER_DENIED_MESSAGE)
    assert panel.calls[0].args["label"] == "worker"
    assert "reason" not in panel.calls[0].args


async def test_relative_paths_anchor_at_workspace_root(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    panel = _Panel(True)
    broker = _broker(Mode.auto, root, panel=panel)
    assert (await broker.handle("r1", _write("src/a.py")))["behavior"] == "allow"
    assert panel.calls == []
    await broker.handle("r2", _write("../escape.py"))
    assert len(panel.calls) == 1  # outside the root once resolved → prompted


async def test_panel_denial_message_reaches_claude(tmp_path: Path):
    panel = _Panel(ToolDenied(message="use the scratchpad instead"))
    broker = _broker(Mode.ask, tmp_path, panel=panel)
    reply = await broker.handle("r1", _write(str(tmp_path / "a.py")))
    assert reply == deny_reply("use the scratchpad instead")


async def test_headless_ask_denies_with_explanation(tmp_path: Path):
    broker = _broker(Mode.ask, tmp_path, panel=None)
    reply = await broker.handle("r1", _write(str(tmp_path / "a.py")))
    assert reply == deny_reply(HEADLESS_DENY_MESSAGE)


async def test_bash_prompts_in_ask_mode_as_marim_bash(tmp_path: Path):
    panel = _Panel(True)
    broker = _broker(Mode.ask, tmp_path, panel=panel)
    req = {
        "subtype": "can_use_tool",
        "tool_name": "Bash",
        "input": {"command": "ls"},
        "tool_use_id": "b1",
    }
    assert (await broker.handle("r1", req))["updatedInput"] == {"command": "ls"}
    assert panel.calls[0].tool_name == "bash" and panel.calls[0].args["command"] == "ls"


async def test_ask_user_question_routes_to_ask_user(tmp_path: Path):
    seen: list = []

    async def ask(questions):
        seen.extend(questions)
        return {"Color": "Blue", "Extras": ["Bold", "Wide"]}

    broker = _broker(Mode.plan, tmp_path, ask_user=ask)  # plan mode does not block questions
    req = {
        "subtype": "can_use_tool",
        "tool_name": "AskUserQuestion",
        "requires_user_interaction": True,
        "input": {
            "questions": [
                {
                    "question": "Which color?",
                    "header": "Color",
                    "options": [{"label": "Red"}, {"label": "Blue", "description": "cool"}],
                    "multiSelect": False,
                },
                {
                    "question": "Any extras?",
                    "header": "Extras",
                    "options": [{"label": "Bold"}, {"label": "Wide"}],
                    "multiSelect": True,
                },
            ]
        },
        "tool_use_id": "q1",
    }
    reply = await broker.handle("r1", req)
    assert reply["behavior"] == "allow"
    assert reply["updatedInput"]["answers"] == {"Which color?": "Blue", "Any extras?": "Bold, Wide"}
    assert reply["updatedInput"]["questions"] == req["input"]["questions"]  # original input kept
    assert [q.header for q in seen] == ["Color", "Extras"]
    assert seen[0].options[1].description == "cool" and seen[1].multi is True


async def test_ask_user_unbound_or_cancelled_denies_with_guidance(tmp_path: Path):
    req = {
        "subtype": "can_use_tool",
        "tool_name": "AskUserQuestion",
        "input": {"questions": [{"question": "q", "header": "H", "options": [{"label": "a"}]}]},
        "tool_use_id": "q1",
    }
    assert await _broker(Mode.auto, tmp_path).handle("r1", req) == deny_reply(NO_USER_MESSAGE)

    async def cancelled(questions):
        return None

    assert await _broker(Mode.auto, tmp_path, ask_user=cancelled).handle("r2", req) == deny_reply(
        NO_USER_MESSAGE
    )


async def test_unsupported_subtype_raises(tmp_path: Path):
    broker = _broker(Mode.auto, tmp_path)
    with pytest.raises(ValueError):
        await broker.handle("r1", {"subtype": "hook_callback"})


async def test_cancelled_prompt_records_cancel_and_reraises(tmp_path: Path):
    panel = _Panel(True)
    panel.release.clear()  # the user never answers
    broker = _broker(Mode.ask, tmp_path, panel=panel)
    task = asyncio.ensure_future(broker.handle("r1", _write(str(tmp_path / "a.py"))))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.last_reply == deny_reply(CANCELLED_MESSAGE)


async def test_prompts_are_serialized_in_arrival_order(tmp_path: Path):
    panel = _Panel(True, True)
    panel.release.clear()
    broker = _broker(Mode.ask, tmp_path, panel=panel)
    first = asyncio.ensure_future(broker.handle("r1", _write(str(tmp_path / "a.py"), "tu1")))
    second = asyncio.ensure_future(broker.handle("r2", _write(str(tmp_path / "b.py"), "tu2")))
    await asyncio.sleep(0.01)
    assert [c.tool_call_id for c in panel.calls] == ["tu1"]  # the second waits for the lock
    panel.release.set()
    await asyncio.gather(first, second)
    assert [c.tool_call_id for c in panel.calls] == ["tu1", "tu2"]
