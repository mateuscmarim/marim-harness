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
    thread_config,
)
from tests.fakes import fake_codex_bin, read_request_log

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


async def test_start_initializes_and_checks_version(tmp_path):
    binary = fake_codex_bin(tmp_path, {})
    server = CodexServer(binary=binary)
    await server.start()
    try:
        assert server.alive
        log = read_request_log(tmp_path)
        assert log[0]["method"] == "initialize"
        assert log[0]["params"]["clientInfo"]["name"] == "marim-harness"
        assert log[1] == {"method": "initialized"}
    finally:
        await server.aclose()


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
        assert turn_id == "turn-1" and handle.current_turn_id == "turn-1"
        events = await _drain_until(handle, "turn/completed")
        methods = [m for m, _ in events]
        assert "item/agentMessage/delta" in methods and "warning" in methods
        assert handle.current_turn_id is None  # cleared on turn/completed
        log = read_request_log(tmp_path)
        start = next(m for m in log if m.get("method") == "thread/start")
        assert start["params"]["developerInstructions"] == "be brief"
        assert start["params"]["config"] == thread_config()
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
