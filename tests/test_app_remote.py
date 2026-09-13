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
    JobPanel,
    NoticeMessage,
    UserMessage,
)
from marim_harness.jobs import Job
from marim_harness.server.attach import RemoteTarget
from marim_harness.server.client import HistorySnapshot, RemoteInfo, RemoteUnavailable
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
        self.answer_error: Exception | None = None
        self.asks_error: Exception | None = None
        self.steer_error: Exception | None = None
        # Jobs (4b): what GET jobs returns (rows as the client builds them,
        # ``result`` = the tail), GET jobs/{id} results, and the scripted
        # verdicts for cancel / resume.
        self.jobs_rows: list[Job] = []
        self.jobs_error: Exception | None = None
        self.outputs: dict[str, str] = {}
        self.cancel_reply = "cancelled job-1"
        self.resume_reply: tuple[str | None, str] | Exception = ("job-9", "")

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
        if self.asks_error is not None:
            error, self.asks_error = self.asks_error, None  # fail once
            raise error
        return list(self.asks)

    async def submit(self, prompt, attachments=None, *, trigger="user") -> str:
        self.calls.append(("submit", prompt, trigger))
        return "t1"

    async def interrupt(self) -> bool:
        self.calls.append("interrupt")
        return True

    async def steer(self, text, attachments=None) -> None:
        self.calls.append(("steer", text))
        if self.steer_error is not None:
            raise self.steer_error

    async def answer_ask(self, ask_id, answer) -> bool:
        self.calls.append(("answer", ask_id, answer))
        if self.answer_error is not None:
            error, self.answer_error = self.answer_error, None  # fail once
            raise error
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

    async def jobs(self) -> list[Job]:
        self.calls.append("jobs")
        if self.jobs_error is not None:
            raise self.jobs_error
        return [Job(**vars(row)) for row in self.jobs_rows]  # fresh rows, like the wire

    async def job_output(self, job_id: str) -> str:
        self.calls.append(("job_output", job_id))
        return self.outputs.get(job_id, f"No job {job_id!r}.")

    async def cancel_job(self, job_id: str) -> str:
        self.calls.append(("cancel_job", job_id))
        return self.cancel_reply

    async def resume_spawn(self, stream_id: str) -> tuple[str | None, str]:
        self.calls.append(("resume", stream_id))
        if isinstance(self.resume_reply, Exception):
            raise self.resume_reply
        return self.resume_reply

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
        # Spec order: GET session → jobs → history → attach(after_seq) → asks.
        assert link.calls[:4] == ["load_session", "jobs", "history", "pending_asks"]
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
        assert link.calls[:4] == ["load_session", "history", "jobs", "pending_asks"]
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
async def test_undelivered_answer_is_reported_and_the_ask_comes_back(tmp_path, monkeypatch):
    """The daemon did not take the verdict (answer_ask raised): the user
    sees why, the spent panel goes, and — the ask still being parked on the
    daemon — a fresh panel is mounted from GET asks to answer again."""
    payload = {"tool_name": "bash", "args": {"command": "ls"}, "tool_call_id": "c1"}
    ask = {"id": "a1", "kind": "approval", "payload": payload, "created": "now"}

    def factory(target, root, *, on_state=None):
        link = _Link(target, root, on_state=on_state)
        link.asks = [ask]
        link.answer_error = RemoteUnavailable("answer not delivered: it broke")
        return link

    monkeypatch.setattr(app_mod, "RemoteSessionHost", factory)
    app = HarnessApp(None, remote=_target(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _settle(pilot, lambda: bool(app.query(ApprovalPanel)), what="the parked approval")
        first = app.query_one(ApprovalPanel)
        first.resolve(True)
        await _settle(
            pilot,
            lambda: any("answer not delivered" in t for t in _texts(app, ErrorMessage)),
            what="the undelivered-answer error",
        )
        await _settle(
            pilot,
            lambda: bool(app.query(ApprovalPanel)) and app.query_one(ApprovalPanel) is not first,
            what="a fresh panel for the still-parked ask",
        )
        assert "a1" in app._ask_panels
        answers = [c for c in app.link.calls if isinstance(c, tuple) and c[0] == "answer"]
        assert answers == [("answer", "a1", {"approve": True})]
        # The retry goes through.
        app.query_one(ApprovalPanel).resolve(False)
        await _settle(
            pilot,
            lambda: (
                len([c for c in app.link.calls if c[0] == "answer" if isinstance(c, tuple)]) == 2
            ),
            what="the second answer",
        )


@pytest.mark.anyio
async def test_failed_asks_read_on_resync_keeps_the_panels(tmp_path, monkeypatch):
    """GET asks failing during a resync is an error to report, not "no
    asks": the mounted panel must survive it."""
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
        link.asks_error = RemoteUnavailable("asks not readable: boom")
        link.feed.push("stream.gap", resync="history")
        await _settle(
            pilot,
            lambda: any("asks not resynced" in t for t in _texts(app, ErrorMessage)),
            what="the failed-read error",
        )
        assert len(app.query(ApprovalPanel)) == 1 and "a1" in app._ask_panels


@pytest.mark.anyio
async def test_undelivered_steer_is_reported(remote):
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput

    app, links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        link.steer_error = RemoteUnavailable("steer not delivered: no running turn to steer")
        app.turns.note_submitted("pretend")
        await app.on_prompt_input_steer(PromptInput.Steer("faster"))
        await pilot.pause()
        assert ("steer", "faster") in link.calls
        assert any("steer not delivered" in t for t in _texts(app, ErrorMessage))


@pytest.mark.anyio
async def test_process_local_commands_post_one_notice(remote):
    app, links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        for command in ("/clear", "/new", "/compact", "/rewind", "/mcp", "/jobs wake"):
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


def _spawn_history(repair_stub: str) -> list:
    """A persisted turn with one background spawn (handed off as job-1) and
    one foreground spawn cut down mid-run (its return is the repair stub)."""
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    return [
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


_REPAIR_STUB = (
    "Tool call was interrupted before completion and did not run (the turn "
    "was aborted). Re-issue it if you still need the result."
)


def _running_job(job_id: str, stream_id: str) -> Job:
    return Job(
        id=job_id,
        kind="agent",
        label=f"general: task {stream_id}",
        status="running",
        stream_id=stream_id,
        started_at="2026-09-13T00:00:00+00:00",
    )


def _spawn_app(
    tmp_path, monkeypatch, configure, *, spawn_history: bool = True
) -> tuple[HarnessApp, list[_Link]]:
    """An attached app with ``configure(link)`` applied before mount (the
    jobs and history reads happen at attach), over the spawn history unless
    ``spawn_history`` is off."""
    links: list[_Link] = []

    def factory(target, root, *, on_state=None):
        link = _Link(target, root, on_state=on_state)
        if spawn_history:
            link.messages = _spawn_history(_REPAIR_STUB)
        configure(link)
        links.append(link)
        return link

    monkeypatch.setattr(app_mod, "RemoteSessionHost", factory)
    return HarnessApp(None, remote=_target(tmp_path)), links


@pytest.mark.anyio
async def test_attached_replay_settles_spawns_from_the_daemons_jobs(tmp_path, monkeypatch):
    """The daemon's registry decides: a "running" sidecar whose stream has a
    live job is the daemon's to finish (re-armed onto that job, still
    pending); one with no live job really was cut down (interrupted,
    resumable), and a running sidecar with no card at all is synthesized —
    exactly the in-process settle, against the mirrored rows."""
    _running_sidecars(tmp_path, "sg-bg", "sg-fg", "sg-ghost")

    def configure(link):
        link.jobs_rows = [_running_job("job-1", "sg-bg")]

    app, _links = _spawn_app(tmp_path, monkeypatch, configure)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.jobs_known
        cards = {w.stream_id: w for w in app.stream.subagents}
        assert cards["sg-bg"].status == "pending"
        assert app.stream.detached_job_ids() == {"job-1"}  # adopted: settles from the mirror
        assert cards["sg-fg"].status == "interrupted"
        assert cards["sg-ghost"].status == "interrupted"  # the orphan, synthesized
        assert "job-1" in str(app.query_one("#job-body").render())


@pytest.mark.anyio
async def test_attached_replay_without_the_daemons_jobs_keeps_running_spawns_pending(
    tmp_path, monkeypatch
):
    """The jobs read failed at attach: the mirror is unsynced, so the replay
    cannot tell a spawn the daemon drives from a dead one and keeps the 4a
    behaviour — nothing flagged, no orphan, no resume affordance — and the
    failure is on screen."""
    _running_sidecars(tmp_path, "sg-bg", "sg-fg", "sg-ghost")

    def configure(link):
        link.jobs_error = RemoteUnavailable("jobs not readable: 500")

    app, _links = _spawn_app(tmp_path, monkeypatch, configure)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.jobs_known
        cards = {w.stream_id: w for w in app.stream.subagents}
        assert cards["sg-bg"].status == "pending"
        assert cards["sg-fg"].status == "done"  # not flipped by the sidecar
        assert "sg-ghost" not in cards
        assert not any(w.status == "interrupted" for w in app.stream.subagents)
        assert any("jobs not readable" in t for t in _texts(app, ErrorMessage))


@pytest.mark.anyio
async def test_jobs_changed_refreshes_the_mirror_and_fills_the_settled_card(tmp_path, monkeypatch):
    """``jobs.changed`` carries nothing: the app re-reads GET jobs, fetches
    the full result for a job whose card is still waiting (the list only
    ships tails), fills the card with it and repaints the panel."""
    _running_sidecars(tmp_path, "sg-bg")

    def configure(link):
        link.jobs_rows = [_running_job("job-1", "sg-bg")]

    app, links = _spawn_app(tmp_path, monkeypatch, configure)
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        card = next(w for w in app.stream.subagents if w.stream_id == "sg-bg")
        assert card.status == "pending"
        settled = _running_job("job-1", "sg-bg")
        settled.status, settled.result = "done", "…the tail"
        settled.finished_at = "2026-09-13T00:01:00+00:00"
        link.jobs_rows = [settled]
        link.outputs = {"job-1": "the full report, longer than the tail"}
        link.feed.push("jobs.changed")
        await _settle(pilot, lambda: card.status == "done", what="the card to settle")
        assert card.report == "the full report, longer than the tail"
        assert ("job_output", "job-1") in link.calls
        assert app.jobs.get("job-1").result == "the full report, longer than the tail"
        assert app.stream.detached_job_ids() == set()
        # A later list read (tails only) never downgrades the fetched result.
        link.feed.push("jobs.changed")
        await _settle(pilot, lambda: link.calls.count("jobs") == 3, what="the second refresh")
        await pilot.pause()
        assert app.jobs.get("job-1").result == "the full report, longer than the tail"
        assert link.calls.count(("job_output", "job-1")) == 1


@pytest.mark.anyio
async def test_failed_jobs_refresh_is_reported_and_keeps_the_mirror(tmp_path, monkeypatch):
    def configure(link):
        link.jobs_rows = [_running_job("job-1", "sg-bg")]

    app, links = _spawn_app(tmp_path, monkeypatch, configure, spawn_history=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        assert [j.id for j in app.jobs.list()] == ["job-1"]
        assert app.query_one(JobPanel).display is True
        link.jobs_error = RemoteUnavailable("jobs not readable: 500")
        link.feed.push("jobs.changed")
        await _settle(
            pilot,
            lambda: any("jobs not refreshed" in t for t in _texts(app, ErrorMessage)),
            what="the refresh error",
        )
        assert [j.id for j in app.jobs.list()] == ["job-1"]  # as it was
        assert app.query_one(JobPanel).display is True


@pytest.mark.anyio
async def test_jobs_commands_go_through_the_link_when_attached(tmp_path, monkeypatch):
    def configure(link):
        link.jobs_rows = [_running_job("job-1", "sg-bg")]
        link.outputs = {"job-1": "(still running)"}

    app, links = _spawn_app(tmp_path, monkeypatch, configure, spawn_history=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        for command in ("/jobs", "/jobs output job-1", "/jobs cancel job-1", "/jobs wake"):
            await dispatch(app, command)
        await pilot.pause()
        posted = _texts(app, AssistantMessage)
        assert any("job-1" in t and "task sg-bg" in t for t in posted)
        assert any("(still running)" in t for t in posted)
        assert any("cancelled job-1" in t for t in posted)
        assert ("job_output", "job-1") in link.calls and ("cancel_job", "job-1") in link.calls
        assert any(
            "/jobs wake needs the session's own process" in t for t in _texts(app, NoticeMessage)
        )
        # The daemon did not answer: reported, not swallowed.
        link.cancel_reply = None

        async def failing(job_id):
            raise RemoteUnavailable("daemon gone")

        link.cancel_job = failing
        await dispatch(app, "/jobs cancel job-1")
        await pilot.pause()
        assert any("/jobs cancel failed: daemon gone" in t for t in _texts(app, ErrorMessage))


@pytest.mark.anyio
async def test_resume_key_when_attached_resumes_on_the_daemon(remote):
    from types import SimpleNamespace

    from marim_harness.interfaces.tui.subagents import SubAgentWidget

    app, links = remote
    async with app.run_test() as pilot:
        await pilot.pause()
        link = links[0]
        errors: list[str] = []
        card = SubAgentWidget("general", "task", "test:remote")
        card.stream_id = "sg-int"
        card.pane = SimpleNamespace(append_error=errors.append)
        card.finish("", status="interrupted")
        # Refused by the daemon's runner: the reason lands in the pane, the
        # card stays interrupted so the user can retry.
        link.resume_reply = (None, "Spawn already finished — nothing to resume.")
        await app.subagents._resume(card)
        assert card.status == "interrupted"
        assert errors == ["Spawn already finished — nothing to resume."]
        # The daemon did not answer: same shape, naming the failure.
        link.resume_reply = RemoteUnavailable("daemon gone")
        await app.subagents._resume(card)
        assert card.status == "interrupted"
        assert errors[-1] == "resume failed: daemon gone"
        # Started: the card is re-armed onto the daemon's new job.
        link.resume_reply = ("job-9", "")
        await app.subagents._resume(card)
        await pilot.pause()
        assert card.status == "pending" and card.job_id == "job-9"
        assert "job-9" in app.stream.detached_job_ids()
        assert [c for c in link.calls if isinstance(c, tuple) and c[0] == "resume"] == [
            ("resume", "sg-int")
        ] * 3
