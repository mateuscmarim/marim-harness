"""Note: this file defines its own `_make_deps`/`_make_harness` copies rather
than importing them from tests/conftest.py — bare `from conftest import ...`
does not resolve in this repo (tests/__init__.py makes the project root, not
tests/, the sys.path entry; verified with ModuleNotFoundError)."""

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.server.supervisor import SessionSupervisor
from marim_harness.server.workspaces import WorkspaceRecord
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
