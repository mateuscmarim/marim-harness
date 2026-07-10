from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets.prefixed import PrefixedToolset

from marim_harness.runtime.harness import Harness
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps, _make_harness, _text_model


class _FakeServer:
    """A stand-in MCP server: an async context manager that can be made to fail
    on enter, so connect()'s per-server degradation can be exercised."""

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.id = name
        self.fail = fail
        self.entered = False

    async def __aenter__(self):
        if self.fail:
            raise RuntimeError("boom")
        self.entered = True
        return self

    async def __aexit__(self, *exc) -> bool:
        self.entered = False
        return False


class _FakeSource:
    """Stand-in for config.ModelSource: builds id-tagged models, no network."""

    def __init__(self) -> None:
        self.built: list[str] = []

    def build(self, model_id: str) -> FunctionModel:
        self.built.append(model_id)
        return _named_model(model_id)

    def label(self, model_id: str) -> str:
        return f"fake/{model_id}"

    @property
    def is_local(self) -> bool:
        return False

    async def list_models(self):
        return []


def _named_model(model_id: str) -> FunctionModel:
    """A model whose every reply names the id it was built for, so a test can
    tell which model actually ran a turn."""
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content=f"from {model_id}")])

    return FunctionModel(fn)


def _switch_harness(tmp_path, *, source=None, summarizer=None, titler=None):
    from marim_harness.runtime.harness import HarnessConfig
    from marim_harness.session import SessionManager

    deps = _make_deps(tmp_path)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    return Harness(
        model=_named_model("startup"), provider=BuiltinToolProvider(), deps=deps,
        instructions="x",
        config=HarnessConfig(
            store=manager.create(), manager=manager,
            model_source=source, model_id="startup",
            summarizer=summarizer, titler=titler,
        ),
    )


@pytest.mark.anyio
async def test_connect_degrades_past_failing_server(tmp_path: Path):
    bad = _FakeServer("bad", fail=True)
    good = _FakeServer("good")
    deps = _make_deps(tmp_path)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[bad, good])

    status = await h.connect()
    # The good server is live; the bad one is reported, not fatal.
    assert good in h.mcp._live_servers
    assert bad not in h.mcp._live_servers
    assert good.entered is True
    assert status["connected"] == ["good"]
    assert status["failed"] and status["failed"][0][0] == "bad"

    await h.aclose()
    assert good.entered is False  # connection closed on shutdown
    assert h.mcp._live_servers == []


@pytest.mark.anyio
async def test_connect_noop_without_servers(tmp_path: Path):
    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)  # no mcp_servers
    status = await h.connect()
    assert status == {"connected": [], "failed": []}
    await h.aclose()  # safe with nothing open


@pytest.mark.anyio
async def test_run_turn_forwards_live_toolsets(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.usage import RunUsage

    deps = _make_deps(tmp_path)
    # LSP disabled: isolates this assertion to MCP forwarding — the LSP toolset
    # (when enabled) is composed in alongside live MCP toolsets, which is covered
    # separately by test_lsp_wiring.py.
    h = _make_harness(_text_model(), deps, provider=BuiltinToolProvider(register_lsp_tools=False))
    sentinel = FunctionToolset(id="mcp1")
    h.mcp._live_servers = [sentinel]

    captured: dict = {}

    async def fake_run(user_prompt, **kwargs):
        captured["toolsets"] = kwargs.get("toolsets")
        return SimpleNamespace(
            all_messages=lambda: [], usage=RunUsage(), output="ok"
        )

    h.agent.run = fake_run
    out = await h.run_turn("hi")
    assert out == "ok"
    # Live servers reach agent.run, prefixed with the server name at compose time.
    (forwarded,) = captured["toolsets"]
    assert isinstance(forwarded, PrefixedToolset)
    assert forwarded.wrapped is sentinel and forwarded.prefix == "mcp1"


@pytest.mark.anyio
async def test_connect_skips_disabled_servers(tmp_path: Path):
    off = _FakeServer("off")
    on = _FakeServer("on")
    deps = _make_deps(tmp_path)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[off, on], mcp_disabled=["off"])

    status = await h.connect()
    assert status["connected"] == ["on"]
    assert off.entered is False  # config-disabled: never launched
    assert on in h.mcp._live_servers
    assert off not in h.mcp._live_servers
    await h.aclose()


@pytest.mark.anyio
async def test_run_turn_omits_disabled_from_toolsets(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.usage import RunUsage

    deps = _make_deps(tmp_path)
    # LSP disabled: see test_run_turn_forwards_live_toolsets above.
    h = _make_harness(_text_model(), deps, provider=BuiltinToolProvider(register_lsp_tools=False))
    live_on, live_off = FunctionToolset(id="on"), FunctionToolset(id="off")
    h.mcp._live_servers = [live_on, live_off]
    h.mcp.disabled = {"off"}

    captured: dict = {}

    async def fake_run(user_prompt, **kwargs):
        captured["toolsets"] = kwargs.get("toolsets")
        return SimpleNamespace(all_messages=lambda: [], usage=RunUsage(), output="ok")

    h.agent.run = fake_run
    await h.run_turn("hi")
    (forwarded,) = captured["toolsets"]  # the disabled one is muted
    assert isinstance(forwarded, PrefixedToolset)
    assert forwarded.wrapped is live_on


@pytest.mark.anyio
async def test_disable_server_keeps_connection_but_mutes(tmp_path: Path):
    srv = _FakeServer("demo")
    deps = _make_deps(tmp_path)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[srv])
    await h.connect()
    assert srv.entered is True

    await h.disable_server("demo")
    assert "demo" in h.mcp.disabled
    assert srv.entered is True  # still connected, just not offered
    await h.aclose()


@pytest.mark.anyio
async def test_enable_server_connects_on_demand(tmp_path: Path):
    srv = _FakeServer("demo")
    deps = _make_deps(tmp_path)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[srv], mcp_disabled=["demo"])
    await h.connect()
    assert srv.entered is False  # started disabled, so not launched

    err = await h.enable_server("demo")
    assert err is None
    assert "demo" not in h.mcp.disabled
    assert srv.entered is True  # connected on demand
    assert srv in h.mcp._live_servers
    assert "demo" in h.mcp.mcp_status.connected
    await h.aclose()


@pytest.mark.anyio
async def test_enable_after_close_does_not_double_list_connected(tmp_path: Path):
    """Re-enabling a server whose name is still in mcp_status.connected (e.g.
    after an aclose that cleared the live list but not the status) must not add a
    duplicate entry."""
    srv = _FakeServer("demo")
    deps = _make_deps(tmp_path)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[srv])
    await h.connect()
    assert h.mcp.mcp_status.connected == ["demo"]
    await h.aclose()

    await h.enable_server("demo")
    assert h.mcp.mcp_status.connected.count("demo") == 1
    await h.aclose()


@pytest.mark.anyio
async def test_toggle_persists_to_config_across_the_session(tmp_path: Path):
    import json as _json

    ppath = tmp_path / ".marim" / "mcp.json"
    ppath.parent.mkdir(parents=True)
    ppath.write_text(
        _json.dumps({"mcpServers": {"demo": {"command": "x"}}}), encoding="utf-8"
    )
    srv = _FakeServer("demo")
    deps = _make_deps(tmp_path)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[srv])
    await h.connect()

    await h.disable_server("demo")
    assert _json.loads(ppath.read_text())["mcpServers"]["demo"]["enabled"] is False

    await h.enable_server("demo")
    assert _json.loads(ppath.read_text())["mcpServers"]["demo"]["enabled"] is True
    await h.aclose()


@pytest.mark.anyio
async def test_enable_server_reports_connection_failure(tmp_path: Path):
    srv = _FakeServer("demo", fail=True)
    deps = _make_deps(tmp_path)
    h = Harness(model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
                instructions="x", mcp_servers=[srv], mcp_disabled=["demo"])
    await h.connect()

    err = await h.enable_server("demo")
    assert err and "boom" in err  # surfaced, not fatal
    assert srv not in h.mcp._live_servers
    await h.aclose()


def test_resume_restores_saved_model(tmp_path: Path):
    from marim_harness.session import SessionManager

    deps = _make_deps(tmp_path)
    manager = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = manager.create()
    first = Harness(model=_named_model("startup"), provider=BuiltinToolProvider(),
                    deps=deps, instructions="x", store=store, manager=manager,
                    model_source=_FakeSource(), model_id="startup")
    first.set_model("openai/gpt-5.2")

    second = Harness(model=_named_model("startup"), provider=BuiltinToolProvider(),
                     deps=deps, instructions="x", store=manager.store(store.session_id),
                     manager=manager, model_source=_FakeSource(), model_id="startup")
    second.resume()
    assert second.model_id == "openai/gpt-5.2"


def test_granted_servers_resolves_named(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    a = SimpleNamespace(id="mddocs")
    b = SimpleNamespace(id="sentry")
    h.mcp._live_servers = [a, b]

    granted, unknown = h.mcp.granted_servers(["mddocs"])
    assert granted == [a]
    assert unknown == []


def test_granted_servers_none_grants_nothing(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [SimpleNamespace(id="mddocs")]

    assert h.mcp.granted_servers(None) == ([], [])
    assert h.mcp.granted_servers([]) == ([], [])


def test_granted_servers_reports_unknown(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [SimpleNamespace(id="mddocs")]

    granted, unknown = h.mcp.granted_servers(["mddocs", "nope"])
    assert granted == [h.mcp._live_servers[0]]
    assert unknown == ["nope"]


def test_granted_servers_excludes_disabled(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [SimpleNamespace(id="mddocs")]
    h.mcp.disabled = {"mddocs"}

    granted, unknown = h.mcp.granted_servers(["mddocs"])
    assert granted == []
    assert unknown == ["mddocs"]


def test_granted_servers_dedupes(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    a = SimpleNamespace(id="mddocs")
    h.mcp._live_servers = [a]

    granted, unknown = h.mcp.granted_servers(["mddocs", "mddocs"])
    assert granted == [a]
    assert unknown == []


def test_granted_servers_dedupes_unknown(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = []

    granted, unknown = h.mcp.granted_servers(["nope", "nope"])
    assert granted == []
    assert unknown == ["nope"]


def test_mcp_grant_note_lists_unknown_and_enabled(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [
        SimpleNamespace(id="mddocs"),
        SimpleNamespace(id="sentry"),
    ]

    note = h.mcp.grant_note(["nope"])
    assert "nope" in note
    assert "mddocs" in note and "sentry" in note
    assert note.endswith("\n\n")


def test_mcp_grant_note_empty_when_nothing_unknown(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    assert h.mcp.grant_note([]) == ""


def test_mcp_grant_note_handles_no_enabled_servers(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = []  # nothing enabled

    note = h.mcp.grant_note(["nope"])
    assert "nope" in note
    assert "none" in note.lower()


def test_mcp_index_text_lists_enabled(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [
        SimpleNamespace(id="mddocs"),
        SimpleNamespace(id="sentry"),
    ]
    text = h.mcp.mcp_index_text()
    assert "mddocs" in text and "sentry" in text
    assert "spawn_agent" in text  # tells the model how to use them


def test_mcp_index_text_silent_when_none(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = []
    assert h.mcp.mcp_index_text() == ""


def test_mcp_index_text_excludes_disabled(tmp_path: Path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    h = _make_harness(TestModel(), deps)
    h.mcp._live_servers = [
        SimpleNamespace(id="mddocs"),
        SimpleNamespace(id="sentry"),
    ]
    h.mcp.disabled = {"sentry"}
    text = h.mcp.mcp_index_text()
    assert "mddocs" in text
    assert "sentry" not in text
