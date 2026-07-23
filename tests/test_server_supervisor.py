"""Note: this file defines its own `_make_deps`/`_make_harness` copies rather
than importing them from tests/conftest.py — bare `from conftest import ...`
does not resolve in this repo (tests/__init__.py makes the project root, not
tests/, the sys.path entry; verified with ModuleNotFoundError)."""

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage

from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.server.supervisor import SessionSupervisor
from marim_harness.server.workspaces import WorkspaceRecord
from marim_harness.session import SessionManager
from marim_harness.tools.provider import BuiltinToolProvider

pytestmark = pytest.mark.anyio

_UI_HOOK_FIELDS = {
    "request_approval", "ask_user", "on_subagent_event", "on_subagent_notice",
    "on_subagent_model", "on_subagent_usage", "detach_fanout", "interactive",
    "notifier",
}


def _make_deps(root: Path, mode: Mode = Mode.auto, **kw) -> Deps:
    ui_kw = {k: kw.pop(k) for k in list(kw) if k in _UI_HOOK_FIELDS}
    return Deps(
        workspace=WorkspaceConfig(root=root, mode=mode),
        ui=UIHooks(**ui_kw) if ui_kw else UIHooks(),
        **kw,
    )


def _make_harness(model, deps, **config_kwargs) -> Harness:
    return Harness(model=model, provider=BuiltinToolProvider(), deps=deps,
                   instructions="You are a coding agent.", **config_kwargs)


def _model() -> FunctionModel:
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    async def stream_fn(messages, info):
        yield "ok"

    return FunctionModel(fn, stream_function=stream_fn)


def _factory(created: list):
    async def factory(workspace: Path, session_id: str, mode: Mode | None):
        created.append((workspace, session_id, mode))
        return _make_harness(_model(), _make_deps(workspace, mode=mode or Mode.auto))

    return factory


def _record(tmp_path) -> WorkspaceRecord:
    (tmp_path / "ws").mkdir(exist_ok=True)
    return WorkspaceRecord(id="ws", name="ws", path=str(tmp_path / "ws"),
                           kind="registered", created="2026-07-06")


async def test_host_for_creates_once_and_reuses(tmp_path):
    created: list = []
    sup = SessionSupervisor(_factory(created))
    record = _record(tmp_path)
    a, b = await asyncio.gather(sup.host_for(record, "s1"), sup.host_for(record, "s1"))
    assert a is b
    assert len(created) == 1  # per-key lock: no double build under concurrency
    assert created[0][1] == "s1"
    assert sup.peek("ws", "s1") is a
    assert sup.peek("ws", "other") is None
    await sup.aclose()


async def test_set_mode_reaches_factory(tmp_path):
    created: list = []
    sup = SessionSupervisor(_factory(created))
    record = _record(tmp_path)
    sup.set_mode("ws", "s1", Mode.plan)
    await sup.host_for(record, "s1")
    assert created[0][2] is Mode.plan
    await sup.aclose()


async def test_set_model_persists_when_idle(tmp_path):
    """No live host for the session (idle) -> set_model persists straight to
    the on-disk session store instead of touching a harness."""
    record = _record(tmp_path)
    manager = SessionManager(Path(record.path))
    store = manager.create("s1")
    store.save([], RunUsage())
    sid = store.session_id

    sup = SessionSupervisor(_factory([]))
    sup.set_model(record, sid, "claude-cli:opus")

    assert manager.store(sid).model == "claude-cli:opus"


async def test_bus_survives_eviction(tmp_path):
    sup = SessionSupervisor(_factory([]), idle_ttl=0.0)
    record = _record(tmp_path)
    host = await sup.host_for(record, "s1")
    bus = sup.bus_for("ws", "s1")
    bus.publish("marker", {})
    await asyncio.sleep(0.05)  # host goes idle
    await sup.evict_idle()
    assert sup.peek("ws", "s1") is None
    assert sup.bus_for("ws", "s1") is bus  # same bus, ring intact
    assert bus.last_seq >= 1
    assert host.harness is not None  # closed, not corrupted


async def test_busy_or_subscribed_hosts_survive_eviction(tmp_path):
    sup = SessionSupervisor(_factory([]), idle_ttl=0.0)
    record = _record(tmp_path)
    await sup.host_for(record, "s1")
    sub = sup.bus_for("ws", "s1").attach()  # live subscriber blocks eviction
    await asyncio.sleep(0.05)
    await sup.evict_idle()
    assert sup.peek("ws", "s1") is not None
    sub.close()
    await sup.aclose()


async def test_close_host(tmp_path):
    sup = SessionSupervisor(_factory([]))
    record = _record(tmp_path)
    await sup.host_for(record, "s1")
    assert await sup.close_host("ws", "s1")
    assert sup.peek("ws", "s1") is None
    assert not await sup.close_host("ws", "s1")


async def test_evicted_host_rejects_late_submit_instead_of_hanging(tmp_path):
    """The bug this fixes: host_for() hands out a host, eviction (idle_ttl=0)
    tears it down, and the caller's late submit() must raise cleanly instead
    of silently enqueuing into a dead worker forever."""
    from marim_harness.server.host import HostClosed

    sup = SessionSupervisor(_factory([]), idle_ttl=0.0)
    record = _record(tmp_path)
    host = await sup.host_for(record, "s1")
    await asyncio.sleep(0.05)
    await sup.evict_idle()
    assert sup.peek("ws", "s1") is None
    with pytest.raises(HostClosed):
        host.submit("hello")


async def test_forget_reclaims_bus_lock_and_mode(tmp_path):
    """forget() is for a session that's been permanently deleted, not idle
    eviction — it must discard the bus (so a later bus_for mints a fresh
    one, proving the old one was actually dropped, not just no-op), plus
    the lock and any stored mode."""
    sup = SessionSupervisor(_factory([]))
    record = _record(tmp_path)
    sup.set_mode("ws", "s1", Mode.plan)
    original_bus = sup.bus_for("ws", "s1")
    await sup.host_for(record, "s1")
    await sup.close_host("ws", "s1")
    assert ("ws", "s1") in sup._locks  # close_host alone leaves the lock resident

    sup.forget("ws", "s1")

    assert sup.bus_for("ws", "s1") is not original_bus
    assert ("ws", "s1") not in sup._locks
    assert ("ws", "s1") not in sup._modes


async def test_host_for_waits_for_concurrent_close_before_rebuilding(tmp_path):
    """close_host now holds the per-key lock through the full aclose(), so a
    host_for() racing a slow close must wait for it to finish rather than
    building a second Harness while the first is still tearing down. This
    is proven by ORDERING (timestamps), not just by object identity/counts —
    a version that pops-then-releases-the-lock-then-acloses would let the
    second build start immediately, before the artificial delay elapses.

    The "second build returned" timestamp is captured from *inside* the task
    that awaits host_for() (via timed_host_for below), not by timing after a
    later `await close_task` in the test body. Timing it after a sequential
    `await close_task; await host_for_task` would bound the recorded time
    below by close_task's own completion regardless of when host_for_task
    actually finished internally — that shape passes even against the buggy
    pop-then-release-lock-then-aclose implementation, which defeats the
    point of the assertion (verified empirically while writing this test)."""
    created: list = []
    timestamps: dict = {}
    sup = SessionSupervisor(_factory(created))
    record = _record(tmp_path)
    host = await sup.host_for(record, "s1")

    original_aclose = host.aclose

    async def slow_aclose():
        await asyncio.sleep(0.1)
        timestamps["aclose_done"] = asyncio.get_running_loop().time()
        await original_aclose()

    host.aclose = slow_aclose

    async def timed_host_for():
        result = await sup.host_for(record, "s1")
        timestamps["second_build_returned"] = asyncio.get_running_loop().time()
        return result

    close_task = asyncio.create_task(sup.close_host("ws", "s1"))
    await asyncio.sleep(0.01)  # let close_host acquire the lock and start aclose
    host_for_task = asyncio.create_task(timed_host_for())

    await close_task
    new_host = await host_for_task

    assert new_host is not host  # rebuilt fresh, not reused mid-teardown
    assert len(created) == 2  # exactly one rebuild, not a race of two builds
    # The decisive assertion: the second build could only complete (timestamped
    # the instant host_for() itself returned, not after some later sequential
    # await) AFTER the slow aclose finished. Without the lock-holds-through-
    # aclose fix, host_for would race ahead and this would be violated
    # (second_build_returned would land near t=0.01-0.03, long before
    # aclose_done at t=0.1).
    assert timestamps["second_build_returned"] >= timestamps["aclose_done"]


async def test_host_for_falls_back_to_persisted_mode(tmp_path):
    """A daemon restart empties the in-memory mode cache; host_for must
    recover the mode written on the session file header instead of silently
    reverting the session to the configured default."""
    from pydantic_ai.usage import RunUsage

    from marim_harness.session.store import SessionManager

    record = _record(tmp_path)
    store = SessionManager(Path(record.path)).store("s1")
    store.mode = "plan"
    store.save([], RunUsage())

    created: list = []
    sup = SessionSupervisor(_factory(created))  # fresh supervisor: cache cold
    await sup.host_for(record, "s1")
    assert created[0][2] is Mode.plan
    await sup.aclose()


async def test_host_for_ignores_invalid_persisted_mode(tmp_path):
    from pydantic_ai.usage import RunUsage

    from marim_harness.session.store import SessionManager

    record = _record(tmp_path)
    store = SessionManager(Path(record.path)).store("s1")
    store.mode = "yolo"  # hand-edited / future-version file
    store.save([], RunUsage())

    created: list = []
    sup = SessionSupervisor(_factory(created))
    await sup.host_for(record, "s1")
    assert created[0][2] is None  # falls back to the configured default
    await sup.aclose()


async def test_in_memory_mode_wins_over_persisted(tmp_path):
    from pydantic_ai.usage import RunUsage

    from marim_harness.session.store import SessionManager

    record = _record(tmp_path)
    store = SessionManager(Path(record.path)).store("s1")
    store.mode = "plan"
    store.save([], RunUsage())

    created: list = []
    sup = SessionSupervisor(_factory(created))
    sup.set_mode("ws", "s1", Mode.auto)
    await sup.host_for(record, "s1")
    assert created[0][2] is Mode.auto
    await sup.aclose()


async def test_busy_sessions_empty_for_idle_hosts(tmp_path):
    sup = SessionSupervisor(_factory([]))
    record = _record(tmp_path)
    await sup.host_for(record, "s1")
    assert sup.busy_sessions("ws") == []  # a live but idle host is not busy
    assert sup.busy_sessions("other") == []
    await sup.aclose()


async def test_close_workspace_reclaims_all_state(tmp_path):
    created: list = []
    sup = SessionSupervisor(_factory(created))
    record = _record(tmp_path)
    host = await sup.host_for(record, "s1")
    sup.bus_for("ws", "s1")
    sup.set_mode("ws", "s2", Mode.auto)  # mode-only entry, no live host
    sup.bus_for("other", "s9")  # a different workspace must be untouched

    await sup.close_workspace("ws")

    assert sup.peek("ws", "s1") is None
    assert sup.bus_peek("ws", "s1") is None  # forgotten, not merely evicted
    assert host.harness is not None  # closed cleanly, not corrupted
    # A rebuilt host after the wipe sees no cached mode for s2.
    await sup.host_for(record, "s2")
    assert created[-1][1:] == ("s2", None)
    assert sup.bus_peek("other", "s9") is not None
    await sup.aclose()
