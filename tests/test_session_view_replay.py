"""Tests for SessionView._replay_parts shared dispatch.

Verifies that the helper used by both replay_history and replay_messages_into
dispatches each shared part type to the correct widget, so behavioral parity
between the two call sites is enforced at the unit level.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.conftest import _make_deps


def _app(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
    harness = Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test")
    return HarnessApp(harness)


def _app_with_store(tmp_path: Path):
    """Like ``_app`` but wired to a real ``SessionStore``: ``finish_replayed_cards``
    early-returns without one (it needs ``store.path``/``store.session_id`` to open
    the sidecar's ``TranscriptStore``), so any test exercising the jobs-history /
    sidecar join needs this fixture instead of the store-less ``_app``."""
    from pydantic_ai.models.test import TestModel

    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.runtime.harness import Harness
    from marim_harness.session import SessionManager
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create("main")
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps,
        instructions="test", store=store, manager=manager,
    )
    return HarnessApp(harness)


def _spawn_meta(stream_id: str, task: str, status: str = "running") -> dict:
    """A v2 sidecar meta dict carrying the keys the runner's checkpoint write
    produces (spec 2026-07-03-subagent-resume, Task 2) — the minimum shape
    ``scan_meta``/``finish_replayed_cards`` read."""
    return {
        "stream_id": stream_id, "type": "general", "task": task, "model": None,
        "mcp": None, "depth": 1, "max_output_chars": None, "isolation": None,
        "status": status,
    }


@pytest.mark.anyio
async def test_replay_parts_text_mounts_assistant_message(tmp_path: Path):
    """TextPart → AssistantMessage mounted; group/solo reset to None."""
    from pydantic_ai.messages import TextPart

    from marim_harness.interfaces.tui.widgets import AssistantMessage

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session
        mounted: list = []

        async def record(w):
            mounted.append(w)

        group, solo = await sv._replay_parts(
            TextPart(content="hello"), None, record, {}, None, None
        )
        assert len(mounted) == 1
        assert isinstance(mounted[0], AssistantMessage)
        assert group is None
        assert solo is None


@pytest.mark.anyio
async def test_replay_parts_empty_text_mounts_nothing(tmp_path: Path):
    """Empty TextPart is skipped — no widget mounted, group/solo unchanged."""
    from pydantic_ai.messages import TextPart

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session
        mounted: list = []

        async def record(w):
            mounted.append(w)

        sentinel = object()
        group, solo = await sv._replay_parts(
            TextPart(content=""), None, record, {}, sentinel, sentinel  # type: ignore[arg-type]
        )
        assert len(mounted) == 0
        # group/solo are unchanged when nothing is mounted
        assert group is sentinel
        assert solo is sentinel


@pytest.mark.anyio
async def test_replay_parts_tool_return_calls_finish(tmp_path: Path):
    """ToolReturnPart looks up the widget in tool_widgets and calls finish()."""
    from pydantic_ai.messages import ToolReturnPart

    from marim_harness.interfaces.tui.widgets import ToolCallWidget

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session

        fake_widget = MagicMock(spec=ToolCallWidget)
        tool_widgets = {"call-abc": fake_widget}

        part = ToolReturnPart(
            tool_name="read_file",
            content="file contents here",
            tool_call_id="call-abc",
        )
        await sv._replay_parts(part, None, None, tool_widgets, None, None)
        fake_widget.finish.assert_called_once()
        # status arg should be "done" for a successful result
        _, kwargs = fake_widget.finish.call_args
        assert kwargs.get("status") == "done"


@pytest.mark.anyio
async def test_replay_parts_tool_return_unknown_id_is_noop(tmp_path: Path):
    """ToolReturnPart with an unrecognised call_id is silently ignored."""
    from pydantic_ai.messages import ToolReturnPart

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session

        part = ToolReturnPart(
            tool_name="read_file",
            content="result",
            tool_call_id="unknown-id",
        )
        # Should not raise
        await sv._replay_parts(part, None, None, {}, None, None)


@pytest.mark.anyio
async def test_replay_parts_spawn_agent_mounts_widget_no_pane(tmp_path: Path):
    """A foreground spawn_agent in _replay_parts mounts a SubAgentWidget but
    never creates a SubAgentDetailHost pane — pane creation is main-log-only
    and lives in replay_history after the _replay_parts call returns."""
    from pydantic_ai.messages import ToolCallPart

    from marim_harness.interfaces.tui.subagents import SubAgentDetailHost, SubAgentWidget

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session
        mounted: list = []

        async def record(w):
            mounted.append(w)

        tool_widgets: dict = {}
        part = ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "claude", "description": "do stuff", "background": False},
            tool_call_id="call-spawn-1",
        )

        host = app.query_one(SubAgentDetailHost)
        panes_before = len(list(host.query("SubAgentPane")))

        await sv._replay_parts(part, None, record, tool_widgets, None, None)

        assert len(mounted) == 1
        assert isinstance(mounted[0], SubAgentWidget)
        # _replay_parts never creates panes — that's replay_history's job
        panes_after = len(list(host.query("SubAgentPane")))
        assert panes_after == panes_before


@pytest.mark.anyio
async def test_parity_replay_history_and_replay_messages_into(tmp_path: Path):
    """Both replay_history and replay_messages_into produce identical widget types
    for a ModelResponse containing TextPart and ToolCallPart.

    This is the key regression guard: any path-specific deviation in _replay_parts
    dispatch would surface here as a type mismatch.
    """
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from textual.containers import VerticalScroll

    from marim_harness.interfaces.tui.subagents import SubAgentDetailHost
    from marim_harness.interfaces.tui.widgets import AssistantMessage, ToolCallWidget

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session

        messages = [
            ModelResponse(parts=[
                TextPart(content="hello"),
                ToolCallPart(
                    tool_name="read_file",
                    args={"path": "foo.py"},
                    tool_call_id="call-parity-1",
                ),
            ])
        ]

        # -- replay_history path: mount to a fresh VerticalScroll --
        fresh_log = VerticalScroll()
        await app.mount(fresh_log)
        app.harness.session.history = messages  # type: ignore[assignment]
        await sv.replay_history(fresh_log)
        rh_types = [type(w) for w in fresh_log.children]

        # -- replay_messages_into path: mount to a SubAgentPane --
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane("parity-pane", "claude", "", "", "")
        await pilot.pause()  # let the pane's initial children mount
        before_pane = len(list(pane.children))
        await sv.replay_messages_into(pane, messages)
        # Only the widgets added by replay_messages_into (after the fixed headers)
        rmi_types = [type(w) for w in list(pane.children)[before_pane:]]

        assert rh_types == rmi_types
        assert rh_types == [AssistantMessage, ToolCallWidget]


@pytest.mark.anyio
async def test_replay_parts_text_resets_group_solo_with_prior_state(tmp_path: Path):
    """TextPart resets group and solo even when they were non-None on entry.

    The original replay_messages_into omitted this reset, so a tool call after
    text output in a sub-agent pane would be incorrectly grouped with tools
    before the text. The shared _replay_parts helper fixes this for both paths.
    """
    from pydantic_ai.messages import TextPart

    from marim_harness.interfaces.tui.widgets import ToolCallWidget

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session

        mounted: list = []

        async def record(w):
            mounted.append(w)

        fake_solo = MagicMock(spec=ToolCallWidget)
        group, solo = await sv._replay_parts(
            TextPart(content="text after tools"), None, record, {}, None, fake_solo
        )
        assert group is None
        assert solo is None


@pytest.mark.anyio
async def test_background_spawn_replays_as_card_and_joins_subagents(tmp_path: Path):
    """A background spawn_agent call must replay as a SubAgentWidget card (not a
    plain tool row) and land in app.stream.subagents — the backing list the ctrl+x
    screen navigates. Regression: background spawns used to fall through to the
    generic ToolCallWidget arm on replay."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.session.history = [
            ModelResponse(parts=[ToolCallPart(
                tool_name="spawn_agent",
                args={"type": "general", "task": "t", "background": True},
                tool_call_id="sg-bg",
            )]),
            ModelRequest(parts=[ToolReturnPart(
                tool_name="spawn_agent",
                content="Started job-3 (agent) — general: t",
                tool_call_id="sg-bg",
            )]),
        ]
        await app.session.render_session("note")
        await pilot.pause()

        assert any(w.stream_id == "sg-bg" for w in app.stream.subagents)
        card = next(w for w in app.stream.subagents if w.stream_id == "sg-bg")
        assert card.detached and card.job_id == "job-3"


@pytest.mark.anyio
async def test_background_spawn_settles_from_jobs_history(tmp_path: Path):
    """A background spawn's ToolReturnPart is only a job-id handoff, not the
    report — the settled status/report must come from the imported jobs history
    (JobRegistry.import_history), joined by stream_id in finish_replayed_cards."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    app = _app_with_store(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.session.history = [
            ModelResponse(parts=[ToolCallPart(
                tool_name="spawn_agent",
                args={"type": "general", "task": "t", "background": True},
                tool_call_id="sg-bg",
            )]),
            ModelRequest(parts=[ToolReturnPart(
                tool_name="spawn_agent",
                content="Started job-3 (agent) — general: t",
                tool_call_id="sg-bg",
            )]),
        ]
        app.harness.deps.jobs.import_history([{
            "id": "job-3", "kind": "agent", "label": "general: t",
            "status": "done", "result_tail": "all good",
            "stream_id": "sg-bg", "finished_at": "t",
        }])
        await app.session.render_session("note")
        await pilot.pause()

        card = next(w for w in app.stream.subagents if w.stream_id == "sg-bg")
        assert card.status == "done" and card.report == "all good"


@pytest.mark.anyio
async def test_foreground_spawn_with_running_sidecar_flips_to_interrupted(tmp_path: Path):
    """A foreground spawn cut down mid-run leaves its main-history ToolReturnPart
    repaired to the resumability stub — which replay finishes as "done" — but its
    sidecar's meta still says "running" (the final write never happened). The
    sidecar is the more trustworthy source, so finish_replayed_cards flips the
    card to interrupted."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    from marim_harness.session import TranscriptStore

    app = _app_with_store(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        store = app.harness.session.store
        assert store is not None
        ts = TranscriptStore(store.path, store.session_id)
        ts.write(
            "sg-fg",
            [ModelRequest(parts=[])],
            2000,
            meta=_spawn_meta("sg-fg", "t", status="running"),
        )

        repair_stub = (
            "Tool call was interrupted before completion and did not run (the turn "
            "was aborted). Re-issue it if you still need the result."
        )
        app.harness.session.history = [
            ModelResponse(parts=[ToolCallPart(
                tool_name="spawn_agent",
                args={"type": "general", "task": "t"},
                tool_call_id="sg-fg",
            )]),
            ModelRequest(parts=[ToolReturnPart(
                tool_name="spawn_agent", content=repair_stub, tool_call_id="sg-fg",
            )]),
        ]
        await app.session.render_session("note")
        await pilot.pause()

        card = next(w for w in app.stream.subagents if w.stream_id == "sg-fg")
        assert card.status == "interrupted"


@pytest.mark.anyio
async def test_running_sidecar_with_no_card_synthesizes_ghost_card(tmp_path: Path):
    """A running sidecar with no matching spawn anywhere in the main history means
    the owning turn itself never persisted (a crash before that turn's save).
    finish_replayed_cards must still surface the work by synthesizing a card from
    the sidecar meta alone — and it must run even though history is empty."""
    from pydantic_ai.messages import ModelResponse

    from marim_harness.interfaces.tui.subagents import SubAgentDetailHost
    from marim_harness.session import TranscriptStore

    app = _app_with_store(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        store = app.harness.session.store
        assert store is not None
        ts = TranscriptStore(store.path, store.session_id)
        ts.write(
            "sg-ghost",
            [ModelResponse(parts=[])],
            2000,
            meta=_spawn_meta("sg-ghost", "ghost task", status="running"),
        )
        assert app.harness.session.history == []  # the crash-before-persist case

        await app.session.render_session("note")
        await pilot.pause()

        ghost = next(w for w in app.stream.subagents if w.stream_id == "sg-ghost")
        assert ghost.status == "interrupted"
        assert ghost.pane is not None
        assert ghost.pane.transcript_loaded is False  # lazy-load still applies
        # The synthesized pane is registered in the detail host too, not just
        # attached to the card — the ctrl+x screen shows it via the host.
        assert app.query_one(SubAgentDetailHost).pane("sg-ghost") is ghost.pane


@pytest.mark.anyio
async def test_replayed_foreground_card_joins_subagents_list(tmp_path: Path):
    """A replayed FOREGROUND card must also join app.stream.subagents — the
    regression this task fixes for the empty ctrl+x screen after a resume (only
    background spawns joined the list before)."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.session.history = [
            ModelResponse(parts=[ToolCallPart(
                tool_name="spawn_agent",
                args={"type": "general", "task": "t"},
                tool_call_id="sg-fg2",
            )]),
            ModelRequest(parts=[ToolReturnPart(
                tool_name="spawn_agent", content="final report", tool_call_id="sg-fg2",
            )]),
        ]
        await app.session.render_session("note")
        await pilot.pause()

        assert any(w.stream_id == "sg-fg2" for w in app.stream.subagents)


@pytest.mark.anyio
async def test_startup_resume_settles_interrupted_card(tmp_path: Path):
    """The STARTUP resume path (``app.on_mount`` — i.e. ``marim --resume``) must
    settle replayed cards exactly like the session-switch path: a spawn killed
    mid-run leaves its sidecar at status "running", and that must surface as an
    interrupted card. Regression: on_mount replayed the restored history but never
    called finish_replayed_cards (that lived only in the switch/rebuild path), so
    on a normal resume killed spawns were invisible and unresumable.

    Drives the real startup seam: history + sidecar are set up BEFORE run_test,
    so on_mount is what replays and settles them (mirrors test_app.py's
    history-before-mount pattern)."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    from marim_harness.session import TranscriptStore

    app = _app_with_store(tmp_path)
    store = app.harness.session.store
    assert store is not None
    ts = TranscriptStore(store.path, store.session_id)
    ts.write(
        "sg-killed",
        [ModelRequest(parts=[])],
        2000,
        meta=_spawn_meta("sg-killed", "long task", status="running"),
    )
    # A foreground spawn whose dangling ToolReturnPart was repaired to the
    # resumability stub — the killed-mid-run shape. Set on history BEFORE mount so
    # on_mount is the code path that replays and (must) settle it.
    repair_stub = (
        "Tool call was interrupted before completion and did not run (the turn "
        "was aborted). Re-issue it if you still need the result."
    )
    app.harness.session.history = [
        ModelResponse(parts=[ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "general", "task": "long task"},
            tool_call_id="sg-killed",
        )]),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="spawn_agent", content=repair_stub, tool_call_id="sg-killed",
        )]),
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        card = next(w for w in app.stream.subagents if w.stream_id == "sg-killed")
        assert card.status == "interrupted"


@pytest.mark.anyio
async def test_render_session_twice_with_spawn_no_duplicate_panes(tmp_path: Path):
    """Rebuilding the log for a session that has a spawn (render_session — the
    shared switch/clear/startup seam) must be pane-safe. Switching away and back
    re-runs render_session, and without clearing the detail host the second run
    re-adds a pane with the same deterministic ``pane_id`` → DuplicateIds crash.
    Regression for the ``/switch`` crash."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    from marim_harness.interfaces.tui.subagents import SubAgentDetailHost

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.session.history = [
            ModelResponse(parts=[ToolCallPart(
                tool_name="spawn_agent",
                args={"type": "general", "task": "t"},
                tool_call_id="sg-dup",
            )]),
        ]
        await app.session.render_session("first")
        await pilot.pause()
        # Switching away and back re-runs render_session for the same spawn.
        await app.session.render_session("second")
        await pilot.pause()

        host = app.query_one(SubAgentDetailHost)
        panes = [p for p in host.query("SubAgentPane") if p.stream_id == "sg-dup"]
        assert len(panes) == 1  # the rebuild cleared the stale pane before re-adding
        assert host.pane("sg-dup") is not None  # still loadable


@pytest.mark.anyio
async def test_still_running_job_card_rearmed_not_interrupted(tmp_path: Path):
    """A background job survives a session switch/rebuild (jobs are process-scoped).
    A card for a STILL-running job has a live registry job while its sidecar still
    says "running" — it must NOT be flagged interrupted. finish_replayed_cards
    re-arms it (adopt_resumed_card) so it stays pending, re-registers in
    tool_widgets, and settles when the job finishes."""
    import asyncio

    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    from marim_harness.session import TranscriptStore

    app = _app_with_store(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # A live-job settle otherwise fires the autonomous-wake digest turn, which
        # is out of scope here (and races app teardown) — this test is about the
        # card's re-arm/settle, not the wake path.
        app.autonomous_wake = False
        store = app.harness.session.store
        assert store is not None
        ts = TranscriptStore(store.path, store.session_id)
        ts.write("sg-live", [ModelRequest(parts=[])], 2000,
                 meta=_spawn_meta("sg-live", "t", status="running"))

        gate = asyncio.Event()

        async def coro():
            await gate.wait()
            return "live report"

        jobs = app.harness.deps.jobs
        job_id = jobs.register("agent", "general: t", coro(), stream_id="sg-live")
        app.harness.session.history = [
            ModelResponse(parts=[ToolCallPart(
                tool_name="spawn_agent",
                args={"type": "general", "task": "t", "background": True},
                tool_call_id="sg-live",
            )]),
            ModelRequest(parts=[ToolReturnPart(
                tool_name="spawn_agent",
                content=f"Started {job_id} (agent) — general: t",
                tool_call_id="sg-live",
            )]),
        ]
        await app.session.render_session("note")
        await pilot.pause()

        card = next(w for w in app.stream.subagents if w.stream_id == "sg-live")
        assert card.status == "pending"  # re-armed, NOT interrupted
        assert app.stream.tool_widgets.get("sg-live") is card

        gate.set()
        await jobs.wait(job_id)
        await asyncio.sleep(0)  # let the job's done-callback settle it
        app.stream.fill_finished_detached_cards(jobs)
        assert card.status == "done"
        assert card.report == "live report"


@pytest.mark.anyio
async def test_nested_pane_replay_settles_child_from_jobs_history(tmp_path: Path):
    """A nested background spawn buried in a parent's transcript replays as a fresh
    pending card only when the pane is lazily loaded — AFTER the main-pass
    finish_replayed_cards already ran. _load_transcript must re-run the settle join
    so the newly created child card fills from jobs history instead of dangling."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    from marim_harness.interfaces.tui.subagents import SubAgentDetailHost
    from marim_harness.session import TranscriptStore

    app = _app_with_store(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        store = app.harness.session.store
        assert store is not None
        ts = TranscriptStore(store.path, store.session_id)
        ts.write("sg-parent", [
            ModelResponse(parts=[ToolCallPart(
                tool_name="spawn_agent",
                args={"type": "general", "task": "child", "background": True},
                tool_call_id="sg-child",
            )]),
            ModelRequest(parts=[ToolReturnPart(
                tool_name="spawn_agent",
                content="Started job-9 (agent) — general: child",
                tool_call_id="sg-child",
            )]),
        ], 2000)
        app.harness.deps.jobs.import_history([{
            "id": "job-9", "kind": "agent", "label": "general: child",
            "status": "done", "result_tail": "child done",
            "stream_id": "sg-child", "finished_at": "t",
        }])
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane("sg-parent", "general", "", "parent", "parent task")

        await app.subagents._load_transcript(pane, "sg-parent")
        await pilot.pause()

        child = next(w for w in app.stream.subagents if w.stream_id == "sg-child")
        assert child.status == "done"
        assert child.report == "child done"


@pytest.mark.anyio
async def test_never_ran_spawn_card_finishes_failed_not_interrupted(tmp_path: Path):
    """A spawn_agent ToolCallPart that never actually executed (Pydantic
    arg-validation retry → a RetryPromptPart, no ToolReturnPart, no sidecar)
    replays as a pending card. With no settled job AND no sidecar meta there is
    nothing to resume (resume_spawn refuses a card with no meta), so the card must
    finish "failed" — not the forever-"interrupted" ghost the old else-branch
    produced."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    app = _app_with_store(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.session.history = [
            ModelResponse(parts=[ToolCallPart(
                tool_name="spawn_agent",
                args={"type": "general", "task": "t"},
                tool_call_id="sg-never",
            )]),
        ]
        await app.session.render_session("note")
        await pilot.pause()

        card = next(w for w in app.stream.subagents if w.stream_id == "sg-never")
        assert card.status == "failed"
        assert "spawn never ran" in card.report


@pytest.mark.anyio
async def test_v1_sidecar_spawn_settles_done_not_never_ran(tmp_path: Path):
    """A pre-envelope (v1 bare-list) sidecar proves the spawn ran and completed
    under the old write-once scheme — scan_meta can't see it (no meta), but the
    settle join must NOT mislabel it "spawn never ran". Regression for resumed
    sessions recorded before the v2 envelope landed: their cards settled failed
    with a bogus never-ran report while the transcript sat readable on disk."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.session import TranscriptStore

    app = _app_with_store(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        store = app.harness.session.store
        assert store is not None
        ts = TranscriptStore(store.path, store.session_id)
        ts.write("sg-legacy", [ModelResponse(parts=[TextPart(content="did it")])],
                 2000)  # no meta → v1 bare list, exactly what pre-envelope code wrote
        app.harness.session.history = [
            ModelResponse(parts=[ToolCallPart(
                tool_name="spawn_agent",
                args={"type": "general", "task": "t", "background": True},
                tool_call_id="sg-legacy",
            )]),
            ModelRequest(parts=[ToolReturnPart(
                tool_name="spawn_agent",
                content="Started job-1 (agent) — general: t",
                tool_call_id="sg-legacy",
            )]),
        ]
        await app.session.render_session("note")
        await pilot.pause()

        card = next(w for w in app.stream.subagents if w.stream_id == "sg-legacy")
        assert card.status == "done"
        assert "spawn never ran" not in card.report


@pytest.mark.anyio
async def test_settle_rehydrates_card_stats_from_meta(tmp_path: Path):
    """A finished v2 sidecar's meta carries usage/tool_count/duration; the settle
    join must rehydrate the card's stats columns from it — otherwise a resumed
    sub-agents screen shows 0 toolcalls / 0 tokens / 0s for work that plainly
    happened."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.session import TranscriptStore

    app = _app_with_store(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        store = app.harness.session.store
        assert store is not None
        ts = TranscriptStore(store.path, store.session_id)
        meta = {**_spawn_meta("sg-stats", "t", status="finished"),
                "usage": {"input": 900, "output": 100},
                "tool_count": 7, "duration": 65.0}
        ts.write("sg-stats", [ModelResponse(parts=[TextPart(content="done")])],
                 2000, meta=meta)
        app.harness.session.history = [
            ModelResponse(parts=[ToolCallPart(
                tool_name="spawn_agent",
                args={"type": "general", "task": "t", "background": True},
                tool_call_id="sg-stats",
            )]),
            ModelRequest(parts=[ToolReturnPart(
                tool_name="spawn_agent",
                content="Started job-1 (agent) — general: t",
                tool_call_id="sg-stats",
            )]),
        ]
        await app.session.render_session("note")
        await pilot.pause()

        card = next(w for w in app.stream.subagents if w.stream_id == "sg-stats")
        assert card.status == "done"
        assert card.tool_count == 7
        assert card.tokens == 1000
        assert card._duration() == "1m 5s"  # frozen from meta, not replay wall-clock
