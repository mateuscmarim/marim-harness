"""``HarnessApp`` attached to a daemon-owned session (phase 4a).

The app is constructed with ``remote=RemoteTarget`` and no Harness; the link
it binds in ``on_mount`` is replaced here by a scripted fake that answers the
same surface ``RemoteSessionHost`` does (``load_session`` / ``history`` /
``attach`` / ``pending_asks`` / the commands). These tests pin the attach
flow's order and the seam's behaviour — what an attached TUI does with a
running turn, a stream gap, a lost daemon and a process-local command — not
the HTTP client, which has its own file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RunUsage

from marim_harness.interfaces.tui import app as app_mod
from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.commands import dispatch
from marim_harness.interfaces.tui.interactions import ApprovalPanel
from marim_harness.interfaces.tui.link import RemoteOnly
from marim_harness.interfaces.tui.widgets import (
    AssistantMessage,
    ErrorMessage,
    NoticeMessage,
    UserMessage,
)
from marim_harness.server.attach import RemoteTarget
from marim_harness.server.client import HistorySnapshot, RemoteInfo
from marim_harness.server.host import HostClosed
from marim_harness.server.schema import Event
from tests.conftest import _settle

SID = "20260101-000000-remote"
HISTORY_SEQ = 7


def _target(root: Path) -> RemoteTarget:
    return RemoteTarget(
        endpoint="http://127.0.0.1:8642",
        token="tok",
        workspace_id="ws0",
        session_id=SID,
        workspace_root=root,
    )


class _Feed:
    def __init__(self, after_seq) -> None:
        self.after_seq = after_seq
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self.closed = False
        self.seq = (after_seq or 0) + 1

    async def next_event(self, timeout=None):
        if timeout is None:
            return await self.queue.get()
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self.closed = True

    def push(self, type_: str, **data) -> None:
        self.queue.put_nowait(Event(seq=self.seq, ts="t", type=type_, data=data))
        self.seq += 1


class _Link:
    """The remote link's surface, scripted."""

    kind = "remote"

    def __init__(self, target: RemoteTarget, root: Path, *, on_state=None) -> None:
        self.target = target
        self.on_state = on_state
        self.info = RemoteInfo(
            workspace_root=root,
            session_id=target.session_id,
            session_name="remote-run",
            mode="auto",
            model_id="test:remote",
            model_label="test:remote",
            usage=RunUsage(input_tokens=30, output_tokens=12),
        )
        self.status_value = "idle"
        self.messages = [
            ModelRequest(parts=[UserPromptPart(content="earlier prompt")]),
            ModelResponse(parts=[TextPart(content="earlier reply")]),
        ]
        self.asks: list[dict] = []
        self.calls: list = []
        self.feeds: list[_Feed] = []
        self.fail_load: Exception | None = None

    async def load_session(self) -> str:
        self.calls.append("load_session")
        if self.fail_load is not None:
            raise self.fail_load
        return self.status_value

    async def history(self) -> HistorySnapshot:
        self.calls.append("history")
        return HistorySnapshot(list(self.messages), HISTORY_SEQ)

    def attach(self, after_seq=None) -> _Feed:
        if self.feeds:
            self.feeds[-1].close()  # as RemoteSessionHost.attach: one live subscription
        feed = _Feed(after_seq)
        self.feeds.append(feed)
        return feed

    async def pending_asks(self) -> list[dict]:
        self.calls.append("pending_asks")
        return list(self.asks)

    async def submit(self, prompt, attachments=None, *, trigger="user") -> str:
        self.calls.append(("submit", prompt, trigger))
        return "t1"

    async def interrupt(self) -> bool:
        self.calls.append("interrupt")
        return True

    async def steer(self, text, attachments=None) -> None:
        self.calls.append(("steer", text))

    async def answer_ask(self, ask_id, answer) -> bool:
        self.calls.append(("answer", ask_id, answer))
        return True

    async def set_mode(self, mode: str) -> None:
        self.calls.append(("set_mode", mode))
        self.info.mode = mode

    async def set_model(self, model_id: str) -> None:
        self.calls.append(("set_model", model_id))

    async def refresh(self) -> None:
        self.calls.append("refresh")

    async def close(self) -> None:
        self.calls.append("close")

    @property
    def feed(self) -> _Feed:
        return self.feeds[-1]


@pytest.fixture()
def remote(tmp_path, monkeypatch):
    """An attached app plus the fake link it bound; the link is created by
    the app's own on_mount through the patched constructor."""
    links: list[_Link] = []

    def factory(target, root, *, on_state=None):
        link = _Link(target, root, on_state=on_state)
        links.append(link)
        return link

    monkeypatch.setattr(app_mod, "RemoteSessionHost", factory)
    app = HarnessApp(None, remote=_target(tmp_path))
    return app, links


def _texts(app, cls) -> list[str]:
    """The visible text of every mounted ``cls``: the markdown source for the
    streaming assistant widget, the Static's renderable otherwise."""
    return [getattr(w, "text", None) or str(w.render()) for w in app.query(cls)]


def test_constructor_takes_exactly_one_of_harness_or_remote(tmp_path):
    with pytest.raises(ValueError):
        HarnessApp(None)
    app = HarnessApp(None, remote=_target(tmp_path))
    assert app.harness is None and app.attached
    with pytest.raises(RemoteOnly, match="/clear needs the session's own process"):
        app.require_local("/clear")


@pytest.mark.anyio
async def test_attach_seeds_the_view_replays_history_and_feeds_from_the_boundary(remote):
    app, links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        # Spec order: GET session → history → attach(after_seq) → GET asks.
        assert link.calls[:3] == ["load_session", "history", "pending_asks"]
        assert link.feed.after_seq == HISTORY_SEQ
        assert app.link is link and app.sub_title == str(link.info.workspace_root)
        assert app.status.mode == "auto"
        assert app.status.link_label == "daemon"
        assert not app.turn_busy and app.turns_idle.is_set()
        # The intro names the session and the replay shows the transcript.
        await _settle(
            pilot,
            lambda: any("earlier reply" in t for t in _texts(app, AssistantMessage)),
            what="the replayed reply",
        )
        intro = [w.text for w in app.query(AssistantMessage) if "Attached" in w.text]
        assert intro and "remote-run" in intro[0] and "2 messages, 42 tokens" in intro[0]
        assert any("earlier prompt" in t for t in _texts(app, UserMessage))
        assert app.history_messages == link.messages
        assert app.session_manager() is not None  # the picker still lists local files
        assert app.jobs is app._remote_jobs
        bar = str(app.query_one("#status-bar").render())
        assert "daemon" in bar and "test:remote" in bar
    assert link.calls[-1] == "close"  # on_unmount closes the link, nothing else


@pytest.mark.anyio
async def test_attach_to_a_running_turn_is_busy_until_the_wire_says_idle(remote, monkeypatch):
    app, links = remote
    real_factory = app_mod.RemoteSessionHost

    def factory(target, root, *, on_state=None):
        link = real_factory(target, root, on_state=on_state)
        link.status_value = "running"
        return link

    monkeypatch.setattr(app_mod, "RemoteSessionHost", factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        assert app.turn_busy and not app.turns_idle.is_set()
        # The turn started before we attached: its start and deltas arrive
        # above the history boundary and render through the normal handlers.
        link.feed.push("turn.started", turn_id="t0", trigger="user", prompt="live one")
        link.feed.push("text.delta", text="streamed after attach")
        link.feed.push("turn.finished", turn_id="t0", interrupted=False, elapsed=1.0)
        link.feed.push("session.status", status="idle")
        await _settle(pilot, lambda: not app.turn_busy, what="the idle edge")
        await _settle(
            pilot,
            lambda: any("streamed after attach" in t for t in _texts(app, AssistantMessage)),
            what="the streamed text",
        )
        assert app.turns_idle.is_set()
        assert "refresh" in link.calls  # the idle edge re-reads the daemon-only fields


@pytest.mark.anyio
async def test_submit_goes_through_the_link_and_latches_until_turn_started(remote):
    app, links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        await app.start_turn("hello daemon")
        assert ("submit", "hello daemon", "user") in link.calls
        assert app.turn_busy  # latched on the returned turn id
        link.feed.push("turn.started", turn_id="t1", trigger="user", prompt="hello daemon")
        link.feed.push("session.status", status="running")
        await _settle(pilot, lambda: app.turns.current == "t1", what="turn.started")
        assert app.turn_busy
        await app.action_cancel_turn()
        assert "interrupt" in link.calls
        link.feed.push("turn.finished", turn_id="t1", interrupted=True, elapsed=0.1)
        link.feed.push("session.status", status="idle")
        await _settle(pilot, lambda: not app.turn_busy, what="idle after interrupt")


@pytest.mark.anyio
async def test_stream_gap_resyncs_from_history_and_reattaches(remote):
    app, links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        first = link.feed
        link.messages.append(ModelRequest(parts=[UserPromptPart(content="missed while away")]))
        link.status_value = "running"
        del link.calls[:]
        first.push("stream.gap", resync="history")
        await _settle(pilot, lambda: len(link.feeds) == 2, what="the re-attach")
        assert first.closed  # link.attach() closes the old feed
        assert link.feed.after_seq == HISTORY_SEQ
        assert link.calls[:3] == ["load_session", "history", "pending_asks"]
        assert app.turn_busy  # the resync took the daemon's word: running
        await _settle(
            pilot,
            lambda: any("missed while away" in t for t in _texts(app, UserMessage)),
            what="the re-rendered transcript",
        )
        assert any("resynced from history" in w.text for w in app.query(AssistantMessage))
        # The pump now reads the new feed.
        link.feed.push("session.status", status="idle")
        await _settle(pilot, lambda: not app.turn_busy, what="idle from the new feed")


@pytest.mark.anyio
async def test_stream_gap_resync_failure_is_reported_not_fatal(remote):
    app, links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        link.fail_load = HostClosed("daemon went away")
        link.feed.push("stream.gap", resync="history")
        await _settle(
            pilot,
            lambda: any("resync failed" in t for t in _texts(app, ErrorMessage)),
            what="the resync error",
        )
        assert len(link.feeds) == 1


@pytest.mark.anyio
async def test_link_state_drives_the_status_label_and_the_lost_notice(remote):
    app, links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        assert link.on_state is not None
        link.on_state("reconnecting")
        assert app.status.link_label == "daemon · reconnecting…"
        link.on_state("connected")
        assert app.status.link_label == "daemon"
        link.on_state("lost")
        assert app.status.link_label == "daemon · lost"
        await pilot.pause()
        errors = _texts(app, ErrorMessage)
        assert any("daemon unreachable" in e and f"marim --session {SID}" in e for e in errors)


@pytest.mark.anyio
async def test_attach_failure_stays_up_with_the_error(tmp_path, monkeypatch):
    def factory(target, root, *, on_state=None):
        link = _Link(target, root, on_state=on_state)
        link.fail_load = HostClosed("refused us")
        return link

    monkeypatch.setattr(app_mod, "RemoteSessionHost", factory)
    app = HarnessApp(None, remote=_target(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert any("attach failed: refused us" in e for e in _texts(app, ErrorMessage))
        assert app.status.link_label == "daemon · lost"
        assert app._feed is None  # never attached a feed


@pytest.mark.anyio
async def test_pending_asks_are_reconciled_on_attach_and_on_resync(tmp_path, monkeypatch):
    payload = {"tool_name": "bash", "args": {"command": "ls"}, "tool_call_id": "c1"}
    ask = {"id": "a1", "kind": "approval", "payload": payload, "created": "now"}

    def factory(target, root, *, on_state=None):
        link = _Link(target, root, on_state=on_state)
        link.asks = [ask]
        return link

    monkeypatch.setattr(app_mod, "RemoteSessionHost", factory)
    app = HarnessApp(None, remote=_target(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        link = app.link
        await _settle(pilot, lambda: bool(app.query(ApprovalPanel)), what="the parked approval")
        assert "a1" in app._ask_panels
        # The tail also carries it: idempotent by id, still one panel.
        link.feed.push("ask.pending", **ask)
        await pilot.pause()
        await pilot.pause()
        assert len(app.query(ApprovalPanel)) == 1
        # Answered elsewhere and its ask.resolved lost in a gap: GET asks no
        # longer lists it, so the resync drops the panel.
        link.asks = []
        link.feed.push("stream.gap", resync="history")
        await _settle(pilot, lambda: not app.query(ApprovalPanel), what="the stale panel to go")
        assert "a1" not in app._ask_panels


@pytest.mark.anyio
async def test_process_local_commands_post_one_notice(remote):
    app, links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        for command in ("/clear", "/new", "/compact", "/rewind", "/mcp", "/jobs"):
            await dispatch(app, command)
        await pilot.pause()
        notices = _texts(app, NoticeMessage)
        assert sum("needs the session's own process" in n for n in notices) == 6
        assert not any(c[0] == "submit" for c in links[0].calls if isinstance(c, tuple))
        # `!` passthrough is process-local too.
        await app._route_submission("!ls", [])
        await pilot.pause()
        assert any("shell passthrough" in n for n in _texts(app, NoticeMessage))


@pytest.mark.anyio
async def test_mode_and_model_commands_go_through_the_link(remote):
    app, links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        await app.action_cycle_mode()
        assert ("set_mode", "plan") in link.calls or ("set_mode", "ask") in link.calls
        assert app.status.mode == link.info.mode
        await dispatch(app, "/mode auto")
        assert link.info.mode == "auto" and app.status.mode == "auto"
        await dispatch(app, "/model other:model")
        assert ("set_model", "other:model") in link.calls


@pytest.mark.anyio
async def test_steer_with_an_image_is_refused_remotely(remote):
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput

    app, links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        app.turns.note_submitted("pretend")
        await app.on_prompt_input_steer(PromptInput.Steer("look", [(b"\x89PNG", "image/png")]))
        await pilot.pause()
        assert any("Steering with an image" in n for n in _texts(app, NoticeMessage))
        assert not any(isinstance(c, tuple) and c[0] == "steer" for c in link.calls)
        await app.on_prompt_input_steer(PromptInput.Steer("plain"))
        assert ("steer", "plain") in link.calls


@pytest.mark.anyio
async def test_session_switch_in_place_is_refused_when_attached(remote):
    app, links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.switch_to_session_id("20260101-000000-other")
        await pilot.pause()
        assert any(
            "marim --session 20260101-000000-other" in n for n in _texts(app, AssistantMessage)
        )


def _running_sidecars(root: Path, *stream_ids: str) -> None:
    """Sidecars checkpointed mid-run ("running", never finalized) for the
    attached session, written where the daemon would write them: beside the
    session file the app's own ``session_manager()`` resolves."""
    from marim_harness.session import SessionManager, TranscriptStore

    manager = SessionManager(root)
    manager.dir.mkdir(parents=True, exist_ok=True)
    ts = TranscriptStore(manager.session_path(SID), SID)
    for sid in stream_ids:
        meta = {"stream_id": sid, "type": "general", "task": f"task {sid}", "status": "running"}
        ts.write(sid, [ModelRequest(parts=[])], 2000, meta=meta)


@pytest.mark.anyio
async def test_attached_replay_leaves_the_daemons_running_spawns_alone(tmp_path, monkeypatch):
    """In process, a "running" sidecar means the spawn died with the process
    that owned it. Attached, the owner is the daemon and it may still be
    driving that spawn (its jobs never reach this process), so the settle
    must not flag a card interrupted, synthesize an orphan card, or dangle a
    resume this process cannot perform."""
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    _running_sidecars(tmp_path, "sg-bg", "sg-fg", "sg-ghost")
    repair_stub = (
        "Tool call was interrupted before completion and did not run (the turn "
        "was aborted). Re-issue it if you still need the result."
    )
    messages = [
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="spawn_agent",
                    args={"type": "general", "task": "task sg-bg", "background": True},
                    tool_call_id="sg-bg",
                ),
                ToolCallPart(
                    tool_name="spawn_agent",
                    args={"type": "general", "task": "task sg-fg"},
                    tool_call_id="sg-fg",
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="spawn_agent",
                    content="Started job-1 (agent) — general: task sg-bg",
                    tool_call_id="sg-bg",
                ),
                ToolReturnPart(tool_name="spawn_agent", content=repair_stub, tool_call_id="sg-fg"),
            ]
        ),
    ]

    def factory(target, root, *, on_state=None):
        link = _Link(target, root, on_state=on_state)
        link.messages = messages  # what GET history returns at attach
        return link

    monkeypatch.setattr(app_mod, "RemoteSessionHost", factory)
    app = HarnessApp(None, remote=_target(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        cards = {w.stream_id: w for w in app.stream.subagents}
        assert cards["sg-bg"].status == "pending"  # still the daemon's to finish
        assert cards["sg-fg"].status == "done"  # not flipped by the sidecar
        assert "sg-ghost" not in cards  # no orphan synthesized
        assert not any(w.status == "interrupted" for w in app.stream.subagents)


@pytest.mark.anyio
async def test_resume_key_when_attached_posts_the_remote_only_notice(remote):
    from marim_harness.interfaces.tui.subagents import SubAgentWidget

    app, _links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        card = SubAgentWidget("general", "task", "test:remote")
        card.stream_id = "sg-int"
        card.finish("", status="interrupted")
        await app.subagents._resume(card)
        await pilot.pause()
        assert card.status == "interrupted"
        assert any(
            "sub-agent resume needs the session's own process" in n
            for n in _texts(app, NoticeMessage)
        )
