from __future__ import annotations

import asyncio

import pytest

from marim_harness.codex.env import CodexUnavailable
from marim_harness.codex.server import (
    CLOSED,
    CodexServer,
    ThreadHandle,
    ThreadOptions,
    TurnOptions,
)
from tests.fakes import fake_codex_bin, read_argv, read_request_log

pytestmark = pytest.mark.anyio


async def _decline(method: str, params: dict) -> dict:
    return {"decision": "decline"}


WS = {
    "type": "workspaceWrite",
    "writableRoots": ["/w"],
    "networkAccess": False,
    "excludeSlashTmp": False,
    "excludeTmpdirEnvVar": False,
}


async def _drain_until(
    handle: ThreadHandle, method: str, timeout: float = 5.0
) -> list[tuple[str, dict]]:
    """Collect events until ``method`` arrives (per-event timeout; 3.10-safe)."""
    got: list[tuple[str, dict]] = []
    while True:
        item = await asyncio.wait_for(handle.events.get(), timeout)
        got.append(item)
        if item[0] == method:
            return got


async def _wait_for_log(tmp_path, count: int, timeout: float = 5.0) -> list[dict]:
    """The fake logs a message when it READS it; a notification the client
    fires and forgets (`initialized`) can still be in the pipe when the
    awaited response before it has already returned — poll briefly."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        log = read_request_log(tmp_path)
        if len(log) >= count or asyncio.get_running_loop().time() > deadline:
            return log
        await asyncio.sleep(0.02)


async def test_start_initializes_and_checks_version(tmp_path):
    binary = fake_codex_bin(tmp_path, {})
    server = CodexServer(binary=binary)
    await server.start()
    try:
        assert server.alive
        log = await _wait_for_log(tmp_path, 2)
        assert log[0]["method"] == "initialize"
        assert log[0]["params"]["clientInfo"]["name"] == "marim-harness"
        assert log[1] == {"method": "initialized"}
    finally:
        await server.aclose()


def test_handle_remembers_the_completed_turn():
    """The bookkeeping `start_turn` relies on when the completion beat it."""
    handle = ThreadHandle(thread_id="t", events=asyncio.Queue(), request_handler=_decline)
    handle.current_turn_id = "turn-9"
    handle.note_turn_completed({"threadId": "t", "turn": {"id": "turn-9", "status": "completed"}})
    assert handle.current_turn_id is None
    assert handle.last_completed_turn_id == "turn-9"
    handle.note_turn_completed({"threadId": "t"})  # malformed: no turn object
    assert handle.last_completed_turn_id == "turn-9"


def test_stale_completion_keeps_the_running_turn_current():
    """An interrupted turn's completion arriving after the next turn started
    must not clear the NEW turn's id — steer/interrupt would go blind."""
    handle = ThreadHandle(thread_id="t", events=asyncio.Queue(), request_handler=_decline)
    handle.current_turn_id = "turn-2"
    handle.note_turn_completed({"threadId": "t", "turn": {"id": "turn-1", "status": "interrupted"}})
    assert handle.current_turn_id == "turn-2"
    assert handle.last_completed_turn_id == "turn-1"
    handle.note_turn_completed({"threadId": "t", "turn": {"id": "turn-2", "status": "completed"}})
    assert handle.current_turn_id is None
    assert handle.last_completed_turn_id == "turn-2"
    # No id at all: nothing to match against, so it clears as it always did.
    handle.current_turn_id = "turn-3"
    handle.note_turn_completed({"threadId": "t"})
    assert handle.current_turn_id is None


async def test_start_rejects_old_version(tmp_path):
    binary = fake_codex_bin(tmp_path, {"userAgent": "codex_cli_rs/0.100.0"})
    server = CodexServer(binary=binary)
    with pytest.raises(CodexUnavailable, match="0.152"):
        await server.start()
    assert not server.alive


async def test_missing_binary_raises_unavailable(tmp_path):
    server = CodexServer(binary=str(tmp_path / "nope"))
    with pytest.raises(CodexUnavailable):
        await server.start()


async def test_thread_and_turn_events_route_to_handle(tmp_path):
    scenario = {
        "turns": [
            [
                {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "hi"}},
                {"notify": "warning", "params": {"message": "careful"}},
            ]
        ]
    }
    server = CodexServer(binary=fake_codex_bin(tmp_path, scenario))
    await server.start()
    try:
        handle = await server.start_thread(
            options=ThreadOptions(
                cwd="/w",
                developer_instructions="be brief",
                model="gpt-5.6-sol",
                sandbox="workspace-write",
                approval_policy="on-request",
                ephemeral=False,
            ),
            request_handler=_decline,
        )
        assert handle.thread_id == "thread-1"
        turn_id = await server.start_turn(
            handle,
            options=TurnOptions(
                inputs=[{"type": "text", "text": "hello", "text_elements": []}],
                model="gpt-5.6-sol",
                effort="medium",
                approval_policy="on-request",
                sandbox_policy=WS,
            ),
        )
        # The completion may already have been dispatched by the time
        # `start_turn` resumed, in which case the handle has no current turn.
        assert turn_id == "turn-1" and handle.current_turn_id in ("turn-1", None)
        events = await _drain_until(handle, "turn/completed")
        methods = [m for m, _ in events]
        assert "item/agentMessage/delta" in methods and "warning" in methods
        # Cleared on turn/completed — whichever of the turn/start response and
        # the completion notification the client processed first.
        assert handle.current_turn_id is None
        assert handle.last_completed_turn_id == "turn-1"
        log = read_request_log(tmp_path)
        start = next(m for m in log if m.get("method") == "thread/start")
        assert start["params"]["developerInstructions"] == "be brief"
        # Isolation is a launch-time override, not a per-thread config map
        # (which Codex would merge into the user's table anyway).
        assert "config" not in start["params"]
        assert start["params"]["ephemeral"] is False
        turn = next(m for m in log if m.get("method") == "turn/start")
        assert turn["params"]["effort"] == "medium"
        assert turn["params"]["sandboxPolicy"] == WS
        assert "outputSchema" not in turn["params"]
    finally:
        await server.aclose()


async def test_effort_none_is_omitted_and_output_schema_forwarded(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {}))
    await server.start()
    try:
        handle = await server.start_thread(
            options=ThreadOptions(
                cwd="/w",
                developer_instructions=None,
                model=None,
                sandbox="read-only",
                approval_policy="never",
                ephemeral=True,
            ),
            request_handler=_decline,
        )
        await server.start_turn(
            handle,
            options=TurnOptions(
                inputs=[],
                model=None,
                effort=None,
                approval_policy="never",
                sandbox_policy={"type": "readOnly", "networkAccess": False},
                output_schema={"type": "object"},
            ),
        )
        await _drain_until(handle, "turn/completed")
        turn = next(m for m in read_request_log(tmp_path) if m.get("method") == "turn/start")
        assert "effort" not in turn["params"] and "model" not in turn["params"]
        assert turn["params"]["outputSchema"] == {"type": "object"}
        start = next(m for m in read_request_log(tmp_path) if m.get("method") == "thread/start")
        assert "developerInstructions" not in start["params"] and "model" not in start["params"]
    finally:
        await server.aclose()


async def test_server_request_routes_to_thread_handler(tmp_path):
    scenario = {
        "turns": [
            [
                {
                    "request": "item/commandExecution/requestApproval",
                    "params": {"itemId": "c1"},
                    "record_as": "approval",
                }
            ]
        ]
    }
    seen: list[str] = []

    async def handler(method: str, params: dict) -> dict:
        seen.append(method)
        return {"decision": "accept"}

    server = CodexServer(binary=fake_codex_bin(tmp_path, scenario))
    await server.start()
    try:
        handle = await server.start_thread(
            options=ThreadOptions(
                cwd="/w",
                developer_instructions=None,
                model=None,
                sandbox="workspace-write",
                approval_policy="untrusted",
                ephemeral=True,
            ),
            request_handler=handler,
        )
        await server.start_turn(
            handle,
            options=TurnOptions(
                inputs=[],
                model=None,
                effort=None,
                approval_policy="untrusted",
                sandbox_policy=WS,
            ),
        )
        await _drain_until(handle, "turn/completed")
        assert seen == ["item/commandExecution/requestApproval"]
        assert any(m.get("approval") == {"decision": "accept"} for m in read_request_log(tmp_path))
    finally:
        await server.aclose()


async def test_interrupt_ends_hanging_turn(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {"turns": [[{"hang": True}]]}))
    await server.start()
    try:
        handle = await server.start_thread(
            options=ThreadOptions(
                cwd="/w",
                developer_instructions=None,
                model=None,
                sandbox="read-only",
                approval_policy="never",
                ephemeral=True,
            ),
            request_handler=_decline,
        )
        await server.start_turn(
            handle,
            options=TurnOptions(
                inputs=[],
                model=None,
                effort=None,
                approval_policy="never",
                sandbox_policy=WS,
            ),
        )
        await asyncio.sleep(0.2)
        await server.interrupt(handle)
        events = await _drain_until(handle, "turn/completed")
        assert events[-1][1]["turn"]["status"] == "interrupted"
    finally:
        await server.aclose()


async def test_steer_requires_active_turn(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {"turns": [[{"hang": True}]]}))
    await server.start()
    try:
        handle = await server.start_thread(
            options=ThreadOptions(
                cwd="/w",
                developer_instructions=None,
                model=None,
                sandbox="read-only",
                approval_policy="never",
                ephemeral=True,
            ),
            request_handler=_decline,
        )
        assert await server.steer(handle, "nope") is False
        await server.start_turn(
            handle,
            options=TurnOptions(
                inputs=[],
                model=None,
                effort=None,
                approval_policy="never",
                sandbox_policy=WS,
            ),
        )
        await asyncio.sleep(0.2)
        assert await server.steer(handle, "also do X") is True
        steer = next(m for m in read_request_log(tmp_path) if m.get("method") == "turn/steer")
        assert steer["params"]["expectedTurnId"] == "turn-1"
        assert steer["params"]["input"][0]["text"] == "also do X"
        await server.interrupt(handle)
        await _drain_until(handle, "turn/completed")
    finally:
        await server.aclose()


async def test_resume_thread_unknown_returns_none(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {"resumable": ["thread-42"]}))
    await server.start()
    try:
        ok = await server.resume_thread(
            "thread-42",
            options=ThreadOptions(
                cwd="/w",
                developer_instructions=None,
                model=None,
                sandbox="read-only",
                approval_policy="never",
            ),
            request_handler=_decline,
        )
        assert ok is not None and ok.thread_id == "thread-42"
        gone = await server.resume_thread(
            "thread-7",
            options=ThreadOptions(
                cwd="/w",
                developer_instructions=None,
                model=None,
                sandbox="read-only",
                approval_policy="never",
            ),
            request_handler=_decline,
        )
        assert gone is None
    finally:
        await server.aclose()


async def test_resume_thread_forwards_ephemeral_like_start_thread(tmp_path):
    """Minor #7 of the final review: `resume_thread`'s wire params must carry
    `ephemeral` symmetrically with `start_thread`'s, so a future resumable
    ephemeral path isn't silently dropped."""
    server = CodexServer(binary=fake_codex_bin(tmp_path, {"resumable": ["thread-42"]}))
    await server.start()
    try:
        handle = await server.resume_thread(
            "thread-42",
            options=ThreadOptions(
                cwd="/w",
                developer_instructions=None,
                model=None,
                sandbox="read-only",
                approval_policy="never",
                ephemeral=True,
            ),
            request_handler=_decline,
        )
        assert handle is not None
        log = read_request_log(tmp_path)
        resume = next(r for r in log if r["method"] == "thread/resume")
        assert resume["params"]["ephemeral"] is True
    finally:
        await server.aclose()


async def test_crash_delivers_closed_with_stderr_and_respawns(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {"turns": [[{"exit": 1}], []]}))
    await server.start()
    try:
        handle = await server.start_thread(
            options=ThreadOptions(
                cwd="/w",
                developer_instructions=None,
                model=None,
                sandbox="read-only",
                approval_policy="never",
                ephemeral=True,
            ),
            request_handler=_decline,
        )
        await server.start_turn(
            handle,
            options=TurnOptions(
                inputs=[],
                model=None,
                effort=None,
                approval_policy="never",
                sandbox_policy=WS,
            ),
        )
        events = await _drain_until(handle, CLOSED)
        assert "fake: dying" in events[-1][1]["stderr"]
        assert not server.alive
        await server.start()  # respawn
        assert server.alive
        assert handle.thread_id not in server.thread_ids  # respawn clears the old generation
        again = await server.start_thread(
            options=ThreadOptions(
                cwd="/w",
                developer_instructions=None,
                model=None,
                sandbox="read-only",
                approval_policy="never",
                ephemeral=True,
            ),
            request_handler=_decline,
        )
        assert again.thread_id == "thread-1"  # a fresh fake process
    finally:
        await server.aclose()


async def test_reap_cancels_reader_before_new_generation_registers(tmp_path):
    """Regression: `_reap()` must fully interrupt a reader stuck in
    `_run_reader`'s stderr-grace wait before returning, so a respawn's
    freshly-registered thread can never receive a stale CLOSED meant for the
    generation that died.

    This exercises `_run_reader`/`_reap` directly against a stub client
    rather than through the fake binary: the race depends on `_reap()`
    observing the reader mid-grace-wait, a window too narrow (a handful of
    microseconds around a real subprocess's stdout EOF) to hit
    deterministically end-to-end.
    """

    async def _noop_handler(method: str, params: dict) -> dict:
        return {}

    class _EofImmediately:
        """Stands in for a JsonRpcClient whose ``run()`` has already seen
        EOF -- the state ``_run_reader`` is in the instant a process dies."""

        async def run(self) -> None:
            return

    server = CodexServer(binary=fake_codex_bin(tmp_path, {}))
    try:
        old_handle = server._register({"id": "thread-old"}, _noop_handler)
        server._client = _EofImmediately()  # type: ignore[assignment]
        # A stderr pump that never finishes on its own, forcing
        # `_run_reader`'s finally into its 0.5s grace wait -- exactly the
        # window `_reap()` must cancel through rather than race past.
        server._stderr_task = asyncio.create_task(asyncio.sleep(100))
        server._reader_task = asyncio.create_task(server._run_reader(server._client))
        await asyncio.sleep(0)  # let the reader task start and enter the grace wait

        await server._reap()
        new_handle = server._register({"id": "thread-new"}, _noop_handler)
        await asyncio.sleep(0.05)  # give a wrongly-surviving reader a chance to run

        assert new_handle.events.empty(), "a respawned thread must never see a stale CLOSED"
        assert old_handle.events.qsize() <= 1
    finally:
        await server.aclose()


async def test_list_models_and_compact(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {}))
    await server.start()
    try:
        models = await server.list_models()
        assert [m["model"] for m in models] == ["gpt-5.6-sol", "gpt-5.4-mini"]
        handle = await server.start_thread(
            options=ThreadOptions(
                cwd="/w",
                developer_instructions=None,
                model=None,
                sandbox="read-only",
                approval_policy="never",
                ephemeral=False,
            ),
            request_handler=_decline,
        )
        await server.compact(handle)
        await _drain_until(handle, "thread/compacted")
    finally:
        await server.aclose()


async def test_aclose_is_idempotent(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {}))
    await server.start()
    await server.aclose()
    await server.aclose()
    assert not server.alive


async def test_timeout_property_reflects_configured_value(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {}), timeout=42.0)
    assert server.timeout == 42.0


async def test_notification_for_unknown_thread_id_is_dropped_not_broadcast():
    """A thread-scoped notification naming a threadId this process no longer
    has registered (e.g. a spawn's trailing item/turn deltas arriving after its
    thread was deregistered on cancellation) must be dropped, never fanned out
    to every OTHER live thread — Important #2 of the final review."""
    server = CodexServer()
    h1 = server._register({"id": "t1"}, _decline)
    h2 = server._register({"id": "t2"}, _decline)
    await server._on_notification("item/agentMessage/delta", {"threadId": "unknown"})
    assert h1.events.empty()
    assert h2.events.empty()


async def test_notification_without_thread_id_still_reaches_all_threads():
    """A notification carrying no threadId at all (a genuinely global event)
    is the one case broadcast is still correct for."""
    server = CodexServer()
    h1 = server._register({"id": "t1"}, _decline)
    h2 = server._register({"id": "t2"}, _decline)
    await server._on_notification("session/configured", {})
    assert h1.events.get_nowait() == ("session/configured", {})
    assert h2.events.get_nowait() == ("session/configured", {})


async def test_thread_ids_tracks_registered_threads(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {}))
    await server.start()
    try:
        assert server.thread_ids == frozenset()
        handle = await server.start_thread(
            options=ThreadOptions(
                cwd="/w",
                developer_instructions=None,
                model=None,
                sandbox="read-only",
                approval_policy="never",
                ephemeral=True,
            ),
            request_handler=_decline,
        )
        assert server.thread_ids == frozenset({handle.thread_id})
    finally:
        await server.aclose()


async def test_start_disables_user_mcp_servers_and_plugins(tmp_path):
    """The app-server is launched with plugins and the apps connector off and
    one `enabled=false` override per MCP server `codex mcp list` reports, in
    the CLI's order — and although the disabled servers still APPEAR in its
    status list, none counts as connected."""
    binary = fake_codex_bin(
        tmp_path, {"mcpServers": ["playwright", "twm-action-items"], "mcpServersConnected": []}
    )
    server = CodexServer(binary=binary)
    await server.start()
    try:
        assert read_argv(tmp_path) == [
            "-c",
            "features.plugins=false",
            "-c",
            "features.apps=false",
            "-c",
            "mcp_servers.playwright.enabled=false",
            "-c",
            "mcp_servers.twm-action-items.enabled=false",
            "app-server",
        ]
        assert await server.connected_mcp_servers() == []
    finally:
        await server.aclose()


async def test_start_survives_mcp_list_failure(tmp_path, caplog):
    """A failing `codex mcp list` must not block the app-server: it starts
    with only the feature overrides, and the gap is logged."""
    binary = fake_codex_bin(
        tmp_path, {"mcpListFails": True, "mcpServers": ["quiet"], "mcpServersConnected": ["leaked"]}
    )
    server = CodexServer(binary=binary)
    with caplog.at_level("WARNING", logger="marim_harness.codex.server"):
        await server.start()
    try:
        assert server.alive
        assert read_argv(tmp_path) == [
            "-c",
            "features.plugins=false",
            "-c",
            "features.apps=false",
            "app-server",
        ]
        assert "mcp list exited 1" in caplog.text
        # `quiet` is listed but idle (no serverInfo/tools) and must not count.
        assert await server.connected_mcp_servers() == ["leaked"]
    finally:
        await server.aclose()


async def test_idle_is_false_while_a_thread_start_is_in_flight():
    """`thread_ids` is empty until `thread/start` answers, so the idle-closer
    must also see the request in flight (review lead: a daemon starting a
    session while another harness closes could lose the new thread)."""
    from marim_harness.codex import server as server_mod

    server = CodexServer(binary="unused")
    entered, release = asyncio.Event(), asyncio.Event()

    class _Rpc:
        async def request(self, method, params, timeout):
            assert method == "thread/start"
            entered.set()
            await release.wait()
            return {"thread": {"id": "t-1"}}

    server._rpc = lambda: _Rpc()  # type: ignore[method-assign]
    assert server.idle
    opts = ThreadOptions(
        cwd="/w",
        developer_instructions=None,
        model=None,
        sandbox="read-only",
        approval_policy="never",
        ephemeral=True,
    )
    task = asyncio.create_task(server.start_thread(options=opts, request_handler=_decline))
    await entered.wait()
    assert not server.idle and server.thread_ids == frozenset()
    # The singleton closer honours it: nothing is torn down mid-start.
    server_mod._shared = server
    try:
        await server_mod.close_shared_server_if_idle()
        assert server_mod._shared is server
        release.set()
        handle = await task
        assert not server.idle and server.thread_ids == {handle.thread_id}
        server.drop_thread(handle)
        assert server.idle
    finally:
        server_mod._shared = None


async def test_close_if_idle_releases_the_singleton_before_closing():
    """A harness resolving the singleton DURING the close must get a fresh
    server, not the one being SIGTERMed — so the slot is cleared first."""
    from marim_harness.codex import server as server_mod

    class _Observing(CodexServer):
        saw_reset = False

        async def aclose(self) -> None:
            self.saw_reset = server_mod._shared is None
            await super().aclose()

    server = _Observing(binary="unused")
    server_mod._shared = server
    try:
        await server_mod.close_shared_server_if_idle()
        assert server.saw_reset and server_mod._shared is None
    finally:
        server_mod._shared = None


# --- collab child adoption (codex/collab.py) ----------------------------------


async def test_adopt_thread_shares_queue_and_handler_and_routes_child_traffic():
    """A child adopted under a parent is a registered thread whose
    notifications land on the PARENT's queue (one consumer loop drains both)
    and whose server requests reach the parent's approval broker."""
    seen: list[tuple[str, dict]] = []

    async def handler(method: str, params: dict) -> dict:
        seen.append((method, params))
        return {"decision": "accept"}

    server = CodexServer()
    parent = server._register({"id": "t1"}, handler)
    child = server.adopt_thread(parent, "c1")
    assert child.parent_id == "t1"
    assert child.events is parent.events
    assert server.thread_ids == frozenset({"t1", "c1"})
    assert not server.idle
    await server._on_notification("item/agentMessage/delta", {"threadId": "c1", "delta": "hi"})
    assert parent.events.get_nowait() == (
        "item/agentMessage/delta",
        {"threadId": "c1", "delta": "hi"},
    )
    reply = await server._on_server_request(
        "item/commandExecution/requestApproval", {"threadId": "c1", "itemId": "i1"}
    )
    assert reply == {"decision": "accept"}
    assert seen == [("item/commandExecution/requestApproval", {"threadId": "c1", "itemId": "i1"})]
    # Idempotent: adopting again hands back the same handle.
    assert server.adopt_thread(parent, "c1") is child


async def test_child_turn_completed_updates_only_the_child_handle():
    server = CodexServer()
    parent = server._register({"id": "t1"}, _decline)
    parent.current_turn_id = "turn-p"
    child = server.adopt_thread(parent, "c1")
    child.current_turn_id = "turn-c"
    await server._on_notification("turn/completed", {"threadId": "c1", "turn": {"id": "turn-c"}})
    assert child.current_turn_id is None
    assert parent.current_turn_id == "turn-p"
    assert parent.events.get_nowait()[0] == "turn/completed"


async def test_drop_thread_cascades_to_adopted_children():
    server = CodexServer()
    parent = server._register({"id": "t1"}, _decline)
    other = server._register({"id": "t2"}, _decline)
    server.adopt_thread(parent, "c1")
    grandchild_parent = server.adopt_thread(parent, "c2")
    server.adopt_thread(grandchild_parent, "g1")
    server.drop_thread(parent)
    assert server.thread_ids == frozenset({"t2"})
    # A dropped child's trailing traffic is dropped, not broadcast.
    await server._on_notification("item/agentMessage/delta", {"threadId": "g1"})
    assert other.events.empty()
    assert parent.events.empty()


async def test_thread_started_with_a_registered_parent_adopts_on_the_reader_task():
    """Codex announces a collab child with ``thread/started`` carrying
    ``parentThreadId``; the server adopts it right there so the child's
    first item / approval request (dispatched before the consumer has seen
    the parent's spawn item) is not dropped as an unknown thread."""

    async def handler(method: str, params: dict) -> dict:
        return {"decision": "accept"}

    server = CodexServer()
    parent = server._register({"id": "t1"}, handler)
    started = {"thread": {"id": "c1", "parentThreadId": "t1", "agentNickname": "scout"}}
    await server._on_notification("thread/started", started)
    assert server.thread_ids == frozenset({"t1", "c1"})
    assert server._threads["c1"].parent_id == "t1" and server._threads["c1"].events is parent.events
    # The announcement itself still lands on the parent's queue (the router
    # reads the nickname off it), and the child's traffic follows.
    assert parent.events.get_nowait() == ("thread/started", started)
    await server._on_notification("item/agentMessage/delta", {"threadId": "c1", "delta": "x"})
    assert parent.events.get_nowait()[1]["threadId"] == "c1"
    # An unknown parent, or a thread already registered: nothing happens.
    orphan = {"thread": {"id": "c2", "parentThreadId": "zz"}}
    await server._on_notification("thread/started", orphan)
    known = {"thread": {"id": "t1", "parentThreadId": "c1"}}
    await server._on_notification("thread/started", known)
    assert server.thread_ids == frozenset({"t1", "c1"})
    assert server._threads["t1"].parent_id is None


async def test_thread_started_records_the_agent_name_on_the_child_handle():
    """The announced nickname (else role) is kept on the child's handle,
    whether the announcement adopted the child or the router already had
    — that is what labels an approval request the child sends before the
    consumer dequeued the announcement. Unknown/top-level threads have no
    label."""

    async def handler(method: str, params: dict) -> dict:
        return {}

    server = CodexServer()
    parent = server._register({"id": "t1"}, handler)
    announced = {"thread": {"id": "c1", "parentThreadId": "t1", "agentNickname": "scout"}}
    await server._on_notification("thread/started", announced)
    assert server.thread_label("c1") == "scout"
    server.adopt_thread(parent, "c2")  # the router got there first
    by_role = {"thread": {"id": "c2", "parentThreadId": "t1", "agentRole": "worker"}}
    await server._on_notification("thread/started", by_role)
    assert server.thread_label("c2") == "worker"
    # A nameless re-announcement keeps the label; a parent handle never gets one.
    nameless = {"thread": {"id": "c2", "parentThreadId": "t1"}}
    await server._on_notification("thread/started", nameless)
    assert server.thread_label("c2") == "worker"
    upside_down = {"thread": {"id": "t1", "parentThreadId": "c1", "agentNickname": "x"}}
    await server._on_notification("thread/started", upside_down)
    assert server.thread_label("t1") is None and server.thread_label("nope") is None


async def test_release_thread_drops_by_id_and_ignores_unknown_ids():
    async def handler(method: str, params: dict) -> dict:
        return {}

    server = CodexServer()
    parent = server._register({"id": "t1"}, handler)
    server.adopt_thread(parent, "c1")
    server.release_thread("nope")
    assert server.thread_ids == frozenset({"t1", "c1"})
    server.release_thread("c1")
    assert server.thread_ids == frozenset({"t1"})
