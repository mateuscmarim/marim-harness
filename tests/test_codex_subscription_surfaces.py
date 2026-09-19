"""Subscription identities and native events at CLI, mounted TUI and HTTP boundaries."""

import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
from textual.widgets import Input, Static

from marim_harness.config.model import MultiModelSource
from marim_harness.server.bus import EventBus
from marim_harness.server.host import SessionHost
from marim_harness.server.http import create_app
from marim_harness.server.supervisor import SessionSupervisor
from marim_harness.server.workspaces import WorkspaceRegistry
from tests._codex_subscription import MODEL, auth_path, source, sse
from tests._codex_subscription import wire as wire  # noqa: F401
from tests.test_codex_subscription_runtime import builder
from tests.test_providers_section import _PaneHost

AUTH = {"Authorization": "Bearer test-token"}


def server(tmp_path):
    async def factory(workspace, session_id, mode, project_memory_root=None):
        from marim_harness.session import SessionManager

        manager = SessionManager(workspace)
        return builder(workspace, store=manager.store(session_id), manager=manager).build()

    registry = WorkspaceRegistry(tmp_path / "registry.json", tmp_path / "managed")
    record = registry.register("fixture", tmp_path)
    supervisor = SessionSupervisor(factory)
    app = create_app(registry=registry, supervisor=supervisor, token="test-token")
    return app, record, supervisor


@pytest.mark.anyio
async def test_catalog_identity(wire, tmp_path, monkeypatch):
    from marim_harness.interfaces.cli import models
    from marim_harness.workspace.catalog import ModelEntry

    src = MultiModelSource({"openai-codex": source()}, "openai-codex")
    monkeypatch.setattr(MultiModelSource, "from_env", lambda: src)
    entries = await src.list_models()
    assert entries == [ModelEntry(MODEL, MODEL, provider="openai-codex")]
    assert entries[0].qualified == f"openai-codex:{MODEL}"
    out = io.StringIO()
    assert await asyncio.to_thread(models.main, ["list", "--json"], out=out) == 0
    assert json.loads(out.getvalue())[0]["provider"] == "openai-codex"
    app, _, _ = server(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["models"][0]["id"] == f"openai-codex:{MODEL}"
    assert not wire.requests


@pytest.mark.anyio
async def test_mobile_subscription_catalog(wire, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from marim_harness.config import load_config
    from marim_harness.workspace.catalog import ModelEntry

    monkeypatch.setenv("MARIM_PROVIDER", "zen-go")
    monkeypatch.delenv("MARIM_MODEL", raising=False)
    monkeypatch.setenv("MARIM_CODEX_SUBSCRIPTION_MODEL", "gpt-6-astra")
    monkeypatch.setenv("OPENCODE_API_KEY", "fake-zen-key")
    zen_catalog = AsyncMock(return_value=[ModelEntry("glm-5.2", "GLM 5.2")])
    monkeypatch.setattr("marim_harness.config.model.fetch_zen_models", zen_catalog)
    app, _, _ = server(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/models")).status_code == 401
        zen_catalog.assert_not_awaited()
        response = await client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    assert {
        "id": "openai-codex:gpt-6-astra",
        "provider": "openai-codex",
        "name": "gpt-6-astra",
    } in response.json()["models"]
    assert {
        "id": "zen-go:glm-5.2",
        "provider": "zen-go",
        "name": "GLM 5.2",
    } in response.json()["models"]
    assert load_config().provider == "zen-go"
    assert load_config().model == "glm-5.2"
    assert MultiModelSource.from_env().default == "zen-go"
    assert wire.requests == []
    assert wire.refreshes == []


@pytest.mark.anyio
async def test_subscription_card(wire):
    app = _PaneHost()
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        card = app.query_one("#prov-card-openai-codex")
        assert not list(card.query(Input))
        assert (
            "subscription"
            in " ".join(str(widget.render()) for widget in card.query(Static)).lower()
        )
    assert not wire.requests


@pytest.mark.anyio
@pytest.mark.parametrize("present", [True, False])
async def test_unverified_credentials(wire, present, monkeypatch):
    from marim_harness.interfaces.tui.providers import ProvidersPane

    if not present:
        auth_path().unlink()
    verifications = []
    monkeypatch.setattr(
        ProvidersPane, "_start_verify", lambda self, name: verifications.append(name)
    )
    app = _PaneHost(model_source=MultiModelSource({"openai-codex": source()}, "openai-codex"))
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        text = str(app.query_one("#prov-status-openai-codex", Static).render()).lower()
        assert (
            ("configured" in text and "unverified" in text) if present else "login required" in text
        )
        assert "connected" not in text
        assert "openai-codex" not in verifications
    assert wire.requests == [] and wire.refreshes == []


def test_setup_documentation():
    text = Path("docs/reference/configuration.md").read_text()
    for required in (
        "codex login",
        "MARIM_PROVIDER=openai-codex",
        "MARIM_MODEL=gpt-6-astra",
        "in memory",
        "restart",
        "native",
        "MARIM_PROVIDER=codex-cli",
    ):
        assert required in text
    children = Path("docs/guides/subagents.md").read_text()
    assert "openai-codex:" in children and "native" in children


def test_evaluation_record():
    """The evaluation guide may only claim what was actually run.

    Until the 2026-09-19 live run this asserted every row still read NOT RUN,
    so that nobody could quietly promote a case to a success without executing
    it. Those rows are real now, so the guard moves rather than disappears —
    relaxing it to "the table exists" would retire the one check that makes the
    page trustworthy. What it still forbids is a bare verdict: an outcome has
    to be one of the two honest ones, a PASS has to carry the observation that
    earned it, an unfilled case has to stay on the page with the reason it is
    unfilled, and the run parameters the guide itself demands have to be
    recorded.
    """
    text = Path("docs/guides/codex-subscription-evaluation.md").read_text()
    for case in (
        "File edit plus repository test",
        "Approval denial",
        "Native child execution",
        "Interrupt/resume",
        "Context reduction followed by recall",
        "Process restart after credential refresh",
    ):
        # "| case | expected | outcome | evidence |" -> cells 3 and 4.
        row = next(line for line in text.splitlines() if line.startswith(f"| {case} |"))
        outcome, evidence = (cell.strip() for cell in row.split("|")[3:5])
        assert outcome in ("PASS", "NOT RUN"), f"{case}: unreadable outcome {outcome!r}"
        # A verdict is worth only as much as the cell beside it: a PASS needs the
        # observation behind it, and an unavailable case stays visible with its
        # reason rather than vanishing from the table.
        assert len(evidence) > 40, f"{case}: {outcome} with no evidence or reason"
    # The run record the guide's own "Run record required for each executed
    # case" section asks for. A PASS with no provenance is not evidence.
    for field in ("| Commit |", "| Date |", "| Model |", "| Permission mode |"):
        assert field in text, f"run parameters missing {field}"
    # Unknown monetary cost must never be restated as zero quota use.
    assert "not evidence of zero quota use" in text


@pytest.mark.anyio
async def test_model_list_statuses(wire, tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "openai-codex")
    monkeypatch.setenv("MARIM_MODEL", MODEL)
    app, _, _ = server(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/models")).status_code == 401
        result = await client.get("/v1/models", headers=AUTH)
        assert result.status_code == 200
        assert result.json()["models"] == [
            {"id": f"openai-codex:{MODEL}", "provider": "openai-codex", "name": MODEL}
        ]
    assert not wire.requests


@pytest.mark.anyio
async def test_model_selection_statuses(wire, tmp_path):
    from marim_harness.session import SessionManager

    app, record, supervisor = server(tmp_path)
    store = SessionManager(tmp_path).create()
    from pydantic_ai.usage import RunUsage

    store.save([], RunUsage())
    path = f"/v1/workspaces/{record.id}/sessions/{store.session_id}/model"
    selection = {"model": f"openai-codex:{MODEL}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.post(path, json=selection)).status_code == 401
        assert (await client.post(path, headers=AUTH, json={})).status_code == 400
        assert (
            await client.post(
                path.replace(store.session_id, "absent"), headers=AUTH, json=selection
            )
        ).status_code == 404
        assert (await client.post(path, headers=AUTH, json=selection)).status_code == 200
        assert SessionManager(tmp_path).store(store.session_id).model == selection["model"]
        host = await supervisor.host_for(record, store.session_id)
        result = await client.post(path, headers=AUTH, json=selection)
        assert result.status_code == 200
        assert host.harness.current_model.system == "openai-codex"
        entered, release = asyncio.Event(), asyncio.Event()

        async def reply(request):
            import httpx2

            entered.set()
            await release.wait()
            return httpx2.Response(
                200, headers={"Content-Type": "text/event-stream"}, content=sse()
            )

        wire.handler = reply
        host.submit("hold turn")
        await asyncio.wait_for(entered.wait(), 10)
        try:
            assert (await client.post(path, headers=AUTH, json=selection)).status_code == 409
        finally:
            release.set()
            await supervisor.aclose()


async def native_events(wire, tmp_path, consumer):
    (tmp_path / "input.txt").write_text("streamed native result")
    wire.replies.extend(
        [
            sse(reasoning="inspect first", tool=("read_file", {"path": "input.txt"})),
            sse(text="finished"),
        ]
    )
    h = builder(tmp_path).build()
    return await consumer(h)


@pytest.mark.anyio
@pytest.mark.parametrize("consumer", ["headless", "server", "tui"])
async def test_native_stream(wire, tmp_path, consumer, monkeypatch):
    from marim_harness.interfaces.cli.headless import run_headless
    from marim_harness.interfaces.tui.app import HarnessApp
    from tests.conftest import _turn_to_idle

    async def consume(h):
        if consumer == "headless":
            output, errors = io.StringIO(), io.StringIO()
            assert await run_headless(h, "inspect", "stream-json", out=output, err=errors) == 0
            frames = [json.loads(line) for line in output.getvalue().splitlines()]
            types = [frame["type"] for frame in frames]
            assert (
                types.index("thinking")
                < types.index("tool_call")
                < types.index("tool_result")
                < types.index("text")
            )
            assert frames[-1]["usage"]["cost_usd"] is None
            return
        if consumer == "tui":
            from marim_harness.interfaces.tui.widgets import (
                AssistantMessage,
                ThinkingWidget,
                ToolCallWidget,
            )
            from marim_harness.server.wire_events import ToolResult

            app = HarnessApp(h)
            received = []
            rendered_results = []
            on_wire = app.stream.on_wire

            async def observe_wire(event):
                await on_wire(event)
                received.append(event)
                if isinstance(event, ToolResult):
                    widget = app.stream.tool_widgets[event.id]
                    # Snapshot after real rendering, before the next event:
                    # later text or turn cleanup cannot satisfy this proof.
                    rendered_results.append((widget.status, widget.result_text))

            monkeypatch.setattr(app.stream, "on_wire", observe_wire)
            async with app.run_test(size=(120, 40)) as pilot:
                initial = set(app.query("ThinkingWidget, ToolCallWidget, AssistantMessage"))
                await _turn_to_idle(app, "inspect")
                await pilot.pause()
                widgets = [
                    widget
                    for widget in app.query("ThinkingWidget, ToolCallWidget, AssistantMessage")
                    if widget not in initial
                ]
                kinds = [type(widget) for widget in widgets]
                assert (
                    kinds.index(ThinkingWidget)
                    < kinds.index(ToolCallWidget)
                    < kinds.index(AssistantMessage)
                )
                assert app.query(AssistantMessage).last().text == "finished"
                types = [event.type for event in received]
                assert (
                    types.index("thinking.delta")
                    < types.index("tool.call")
                    < types.index("tool.result")
                    < types.index("text.delta")
                )
                result = received[types.index("tool.result")]
                assert isinstance(result, ToolResult)
                assert "streamed native result" in str(result.content)
                assert len(rendered_results) == 1
                assert rendered_results[0][0] == "done"
                assert "streamed native result" in rendered_results[0][1]
            return
        bus = EventBus()
        host = SessionHost(h, bus)
        sub = bus.attach()
        host.submit("inspect")
        types = []
        try:
            while True:
                event = await sub.next_event(timeout=10)
                assert event is not None
                types.append(event.type)
                if event.type == "turn.finished":
                    break
                assert event.type != "turn.error"
            assert (
                types.index("thinking.delta")
                < types.index("tool.call")
                < types.index("tool.result")
                < types.index("text.delta")
            )
        finally:
            sub.close()
            await host.aclose()

    await native_events(wire, tmp_path, consume)
