"""Phase 3b: the TUI's turn lifecycle is driven by bus events, not by awaiting
the turn. ``turn.started`` / ``turn.finished`` / ``turn.error`` drive the
transcript, ``session.status`` drives state, and the app only ever calls
``host.submit()``. These tests cover the seams the migration added on top of
the per-feature tests in test_app.py."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.widgets import NoticeMessage, PromptInput, UserMessage
from tests.conftest import _make_deps, _ok_outcome, _settle, _spy_submit, _turn_to_idle


def _app(tmp_path: Path, reply: str | None = None) -> HarnessApp:
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
    model = TestModel(call_tools=[], custom_output_text=reply)
    harness = Harness(model, BuiltinToolProvider(), deps, instructions="test")
    return HarnessApp(harness)


def _log_kinds(app, *kinds):
    """Direct children of the transcript, in order (the intro header nests an
    AssistantMessage of its own, so a deep walk would count it)."""
    log = app.query_one("#log")
    return [type(w) for w in log.children if isinstance(w, kinds)]


@pytest.mark.anyio
async def test_transcript_order_comes_from_the_wire(tmp_path: Path):
    """The user bubble is mounted off turn.started (not by the submitter), so
    bus order alone puts it above the reply, with the duration stamp last."""
    from marim_harness.interfaces.tui.widgets import AssistantMessage, TurnMeta

    app = _app(tmp_path, reply="the reply")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert list(app.query(UserMessage)) == []  # nothing until the wire says so
        await _turn_to_idle(app, "the prompt")
        assert _log_kinds(app, UserMessage, AssistantMessage, TurnMeta) == [
            UserMessage,
            AssistantMessage,
            TurnMeta,
        ]
        assert "the prompt" in str(app.query_one(UserMessage).render())
        assert app.status.busy is False
        assert app.turn_busy is False


@pytest.mark.anyio
async def test_staged_prompt_drains_after_the_idle_edge(tmp_path: Path):
    """End to end: a prompt typed while a turn runs is staged in the TUI queue
    and submitted to the host on the first turn's idle edge — never earlier,
    never twice."""
    release = asyncio.Event()
    calls: list[str] = []

    async def fake_run_turn(prompt, *a, **k):
        calls.append(prompt)
        if len(calls) == 1:
            await release.wait()
        return _ok_outcome()

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.run_turn = fake_run_turn  # type: ignore[method-assign]
        submitted = _spy_submit(app)
        await app.on_prompt_input_submitted(PromptInput.Submitted("first", []))
        await _settle(pilot, lambda: calls == ["first"], what="the first turn to start")
        await app.on_prompt_input_submitted(PromptInput.Submitted("second", []))
        assert [m.text for m in app.queue.items] == ["second"]
        assert submitted == [("first", "user")]  # staged, not submitted
        release.set()
        await _settle(pilot, lambda: calls == ["first", "second"], what="the drain")
        assert submitted == [("first", "user"), ("second", "user")]
        assert app.queue.items == []
        await asyncio.wait_for(app.turns_idle.wait(), 10)
        users = [str(w.render()) for w in app.query(UserMessage)]
        assert [u for u in users if "first" in u] and [u for u in users if "second" in u]
        assert users.index(next(u for u in users if "first" in u)) < users.index(
            next(u for u in users if "second" in u)
        )


@pytest.mark.anyio
async def test_turn_started_trigger_decides_the_bubble(tmp_path: Path):
    """The same turn.started handler serves every submitter: a user prompt
    gets its bubble, a system prompt shows nothing, an autonomous wake shows
    why the agent woke."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bus = app.host.bus
        bus.publish("turn.started", {"turn_id": "s1", "prompt": "save this", "trigger": "system"})
        bus.publish("turn.started", {"turn_id": "a1", "prompt": "", "trigger": "autonomous"})
        bus.publish("turn.started", {"turn_id": "u1", "prompt": "typed", "trigger": "user"})
        await _settle(pilot, lambda: bool(app.query(UserMessage)), what="the user bubble")
        users = [str(w.render()) for w in app.query(UserMessage)]
        assert users == ["typed"] or (len(users) == 1 and "typed" in users[0])
        assert any("Resumed" in str(n.render()) for n in app.query(NoticeMessage))
        assert not any("save this" in str(n.render()) for n in app.query(NoticeMessage))


@pytest.mark.anyio
async def test_ask_answered_elsewhere_dismisses_with_a_notice(tmp_path: Path):
    """An ask this client is showing, resolved by another client: the panel
    comes down and a notice says which way it went. An interrupt's cancel
    dismisses silently — the "turn cancelled" card already says it all."""
    from marim_harness.interfaces.tui.interactions import ApprovalPanel

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bus = app.host.bus
        payload = {"tool_name": "bash", "args": {"command": "ls"}, "tool_call_id": "c1"}
        bus.publish(
            "ask.pending", {"id": "a1", "kind": "approval", "payload": payload, "created": "now"}
        )
        await _settle(pilot, lambda: bool(app.query(ApprovalPanel)), what="the approval panel")
        bus.publish("ask.resolved", {"id": "a1", "answer": {"approve": True}})
        await _settle(pilot, lambda: not app.query(ApprovalPanel), what="the panel to unmount")
        notices = [str(n.render()) for n in app.query(NoticeMessage)]
        assert any("Approval granted from another client" in n for n in notices)
        assert "a1" not in app._ask_panels

        bus.publish(
            "ask.pending", {"id": "a2", "kind": "approval", "payload": payload, "created": "now"}
        )
        await _settle(pilot, lambda: bool(app.query(ApprovalPanel)), what="the second panel")
        bus.publish("ask.resolved", {"id": "a2", "cancelled": True, "reason": "interrupted"})
        await _settle(pilot, lambda: not app.query(ApprovalPanel), what="the silent dismissal")
        elsewhere = [n for n in app.query(NoticeMessage) if "another client" in str(n.render())]
        assert len(elsewhere) == 1  # the cancel added none


def test_answered_elsewhere_wording_per_panel_kind():
    from marim_harness.interfaces.tui.app import _answered_elsewhere
    from marim_harness.interfaces.tui.interactions import ApprovalPanel, AskUserPanel, PlanCard

    approval = ApprovalPanel.__new__(ApprovalPanel)
    ask = AskUserPanel.__new__(AskUserPanel)
    plan = PlanCard.__new__(PlanCard)
    granted = "Approval granted from another client"
    denied = "Approval denied from another client"
    assert _answered_elsewhere(approval, {"approve": True}) == granted
    assert _answered_elsewhere(approval, {"approve": False}) == denied
    assert _answered_elsewhere(approval, None) == denied
    assert _answered_elsewhere(ask, {"Pick": "Alpha"}) == "Question answered from another client"
    assert _answered_elsewhere(plan, {"approve": True}) == "Plan decided from another client"


@pytest.mark.anyio
async def test_steer_accepted_on_the_wire_renders_the_notice(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bus = app.host.bus
        bus.publish("steer.accepted", {"text": "go left", "attachments": 2})
        await _settle(
            pilot,
            lambda: any("go left" in str(n.render()) for n in app.query(NoticeMessage)),
            what="the steer notice",
        )
        notices = [str(n.render()) for n in app.query(NoticeMessage)]
        notice = next(n for n in notices if "go left" in n)
        assert "↪ steering: go left" in notice and "📎 2" in notice


@pytest.mark.anyio
async def test_unmount_stops_the_host_before_the_snappy_teardown(tmp_path: Path):
    """The TUI keeps its own exit sequence (cancel autoname, persist, ...) but
    the host's worker must be stopped first, or a turn still running would
    keep publishing into a pump that is gone and race the final persist."""
    started = asyncio.Event()

    async def hang(*a, **k):
        started.set()
        await asyncio.sleep(3600)

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.run_turn = hang  # type: ignore[method-assign]
        await app.start_turn("never finishes")
        await _settle(pilot, started.is_set, what="the turn to start")
        host = app.host
    assert host._closing is True
    assert host._worker.done()
    assert host._turn_task is None
    assert app._pump_task is not None and app._pump_task.done()
