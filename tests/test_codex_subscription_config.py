"""Frozen subscription selection/auth/accounting checks, through public upstream APIs."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UserError
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.usage import RunUsage

from marim_harness.config import build_model, load_config
from marim_harness.config.model import ModelConfig, MultiModelSource
from tests._codex_subscription import ACCESS, MODEL, REFRESH, auth_path, harness, source, sse
from tests._codex_subscription import wire as wire  # noqa: F401


def test_explicit_model(wire, monkeypatch, tmp_path):
    from marim_harness.runtime.bootstrap import build_harness

    monkeypatch.setenv("MARIM_PROVIDER", "openai-codex")
    monkeypatch.setenv("MARIM_MODEL", MODEL)
    monkeypatch.setenv("MARIM_LSP", "0")
    monkeypatch.setenv("MARIM_FORGE", "0")
    cfg = load_config()
    assert cfg.provider == "openai-codex"
    model = build_model(cfg)
    assert isinstance(model, OpenAICodexModel)
    assert model.model_name == "gpt-6-astra"
    built = build_harness(tmp_path)
    assert isinstance(built.current_model, OpenAICodexModel)
    assert built.model_id == "openai-codex:gpt-6-astra"
    assert wire.requests == []


@pytest.mark.parametrize("credentials", ["present", "missing"])
def test_qualified_routing(wire, monkeypatch, credentials):
    from marim_harness.config.model import ModelSource

    paid = ModelSource(ModelConfig(provider="openrouter", model="paid-default", api_key="paid-key"))
    monkeypatch.setattr(paid, "build", lambda _: pytest.fail("OpenRouter fallback selected"))
    sources = MultiModelSource(
        {"openrouter": paid},
        "openrouter",
    )
    if credentials == "missing":
        auth_path().unlink()
        with pytest.raises(UserError, match="codex login"):
            sources.build("openai-codex:gpt-6-astra")
        assert sources.sources["openai-codex"].cfg.provider == "openai-codex"
        assert sources.label("openai-codex:gpt-6-astra") == "openai-codex:gpt-6-astra"
        assert wire.requests == []
        assert wire.refreshes == []
        return
    model = sources.build(f"openai-codex:{MODEL}")
    assert isinstance(model, OpenAICodexModel)
    assert model.model_name == "gpt-6-astra"
    assert model.system == "openai-codex"
    assert sources.label(f"openai-codex:{MODEL}") == "openai-codex:gpt-6-astra"


@pytest.mark.parametrize("model", [None, "", "   "])
def test_missing_model(wire, model):
    with pytest.raises(ValueError, match="MARIM_MODEL|qualified"):
        build_model(ModelConfig(provider="openai-codex", model=model))
    assert not wire.requests


def test_existing_providers(wire):
    from marim_harness.config.codex_cli_model import CodexCliModel

    assert load_config().provider == "openrouter"
    assert load_config().model == "anthropic/claude-sonnet-4-6"
    assert isinstance(build_model(ModelConfig(provider="codex-cli", model=MODEL)), CodexCliModel)


@pytest.mark.anyio
async def test_no_cli_process(wire, tmp_path):
    src = source()
    entries = await src.list_models()
    assert [entry.id for entry in entries] == [MODEL]
    wire.replies.append(sse())
    h = harness(tmp_path, src=src)
    assert (await h.run_turn("hello")).result == "done"
    assert len(wire.requests) == 1
    await h.aclose()


@pytest.mark.anyio
async def test_subscription_endpoint(wire, monkeypatch):
    for key in ("OPENAI_API_KEY", "MARIM_API_KEY", "MARIM_BASE_URL"):
        monkeypatch.setenv(key, "https://forbidden.invalid/API-BILLING-SECRET")
    wire.replies.append(sse())
    await Agent(source().build(MODEL)).run("hello")
    req = wire.requests[0]
    assert req["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert req["headers"]["authorization"] == f"Bearer {ACCESS}"
    assert "API-BILLING-SECRET" not in json.dumps(req)
    assert req["json"]["store"] is False


@pytest.mark.anyio
async def test_oauth_request(wire):
    wire.replies.append(sse(text="oauth worked"))
    assert (await Agent(source().build(MODEL)).run("hello")).output == "oauth worked"
    assert wire.requests[0]["headers"]["authorization"] == f"Bearer {ACCESS}"
    assert wire.requests[0]["headers"]["chatgpt-account-id"] == "fixture-account"


@pytest.mark.anyio
@pytest.mark.parametrize("credentials", ["absent", "malformed", "api-key-only", "rejected-refresh"])
async def test_auth_failure(wire, tmp_path, monkeypatch, credentials):
    monkeypatch.setenv("OPENAI_API_KEY", "paid-fallback-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "paid-fallback-key")
    if credentials == "absent":
        auth_path().unlink()
    elif credentials == "malformed":
        auth_path().write_text("{invalid-json")
    elif credentials == "api-key-only":
        auth_path().write_text(json.dumps({"OPENAI_API_KEY": "paid-fallback-key"}))

    if credentials != "rejected-refresh":
        # The public upstream loader rejects invalid credentials during setup.
        with pytest.raises(UserError, match="codex login"):
            harness(tmp_path)
        assert wire.requests == []
        assert wire.refreshes == []
        return

    h = harness(tmp_path)
    assert isinstance(h.current_model, OpenAICodexModel)
    assert wire.requests == []
    wire.replies.extend([(401, {"error": "expired"}), (400, {"error": "invalid_grant"})] * 3)
    try:
        with pytest.raises(RuntimeError, match="codex login"):
            await h.run_turn("rejected refresh")
        assert len(wire.requests) == 3
        assert {req["url"] for req in wire.requests} == {
            "https://chatgpt.com/backend-api/codex/responses"
        }
        assert len(wire.refreshes) == 3
        assert {req["url"] for req in wire.refreshes} == {"https://auth.openai.com/oauth/token"}
    finally:
        await h.aclose()


@pytest.mark.anyio
async def test_refresh_read_only(wire):
    before = auth_path().read_bytes()
    wire.replies.extend(
        [
            (401, {"error": "expired"}),
            (200, {"access_token": "rotated-access", "refresh_token": "rotated-refresh"}),
            sse(),
        ]
    )
    model = source().build(MODEL)
    await Agent(model).run("hello")
    assert len(wire.refreshes) == 1
    assert wire.requests[-1]["headers"]["authorization"] == "Bearer rotated-access"
    assert model.provider.credentials.access_token == "rotated-access"
    assert auth_path().read_bytes() == before


def test_trusted_auth_location(wire, tmp_path, monkeypatch):
    import os

    from marim_harness.config.env import load_environment

    trusted = os.environ["CODEX_HOME"]
    monkeypatch.setenv("MARIM_PROVIDER", "openai-codex")
    monkeypatch.setenv("MARIM_MODEL", MODEL)
    (tmp_path / ".env").write_text(
        "CODEX_HOME=/evil\nMARIM_PROVIDER=local\nMARIM_BASE_URL=https://evil\nOPENAI_API_KEY=evil\n"
    )
    monkeypatch.chdir(tmp_path)
    load_environment()
    assert os.environ["CODEX_HOME"] == trusted
    assert load_config().provider == "openai-codex"
    assert load_config().base_url is None
    assert "OPENAI_API_KEY" not in os.environ


@pytest.mark.anyio
async def test_shared_refresh(wire):
    src = source()
    first, second = src.build(MODEL), src.build("gpt-5.6-luna")
    assert first.provider is second.provider
    arrived = asyncio.Event()
    old_requests = 0

    async def reply(request):
        nonlocal old_requests
        if request.url.host == "auth.openai.com":
            return httpx2.Response(200, json={"access_token": "new", "refresh_token": "new-r"})
        if request.headers["authorization"] == f"Bearer {ACCESS}":
            old_requests += 1
            if old_requests == 2:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), 5)
            return httpx2.Response(401, json={"error": "expired"})
        return httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, content=sse())

    wire.handler = reply
    results = await asyncio.gather(Agent(first).run("first"), Agent(second).run("second"))
    assert [r.output for r in results] == ["done", "done"]
    assert len(wire.refreshes) == 1
    assert len(wire.requests) == 4


def test_subscription_usage(wire, monkeypatch):
    from marim_harness import usage

    monkeypatch.setattr(usage, "estimate_cost", lambda *args: 42.0)
    totals = RunUsage(
        input_tokens=123000,
        output_tokens=30,
        cost=Decimal("19"),
        details={"cost_micro_usd": 2300000, "estimated_cost_micro_usd": 19000000},
    )
    summary = usage.usage_summary(totals, f"openai-codex:{MODEL}")
    assert summary["input_tokens"] == 123000
    assert summary["output_tokens"] == 30
    assert summary["cost_usd"] is None
    assert summary["cost_is_exact"] is False


@pytest.mark.anyio
async def test_offline_transport(wire):
    async with httpx2.AsyncClient() as client:
        with pytest.raises(AssertionError, match="Unexpected live HTTP"):
            await client.get("https://forbidden.invalid")
    with pytest.raises(AssertionError, match="Unexpected Codex subprocess"):
        await asyncio.create_subprocess_exec("codex", "--version")
    with pytest.raises(AssertionError, match="Unexpected HTTP request"):
        await wire.handle(
            httpx2.Request("POST", "https://chatgpt.com/backend-api/codex/responses", json={})
        )


def test_opt_in_only(wire):
    from marim_harness.config.model import KNOWN_PROVIDERS

    assert load_config().provider == "openrouter"
    assert {"openai-codex", "codex-cli"} <= KNOWN_PROVIDERS


def test_dependency_contract():
    text = Path("pyproject.toml").read_text()
    assert '"pydantic-ai-slim[openai,google,mcp]>=2.44.0,<3"' in text
    assert '"pydantic-ai-harness==0.31.0"' in text
    assert 'requires-python = ">=3.10"' in text
    assert '"openai-codex' not in text


@pytest.mark.anyio
async def test_auth_redaction(wire, tmp_path):
    from marim_harness.runtime.builder import HarnessBuilder
    from marim_harness.runtime.errors import format_provider_error

    secrets = [ACCESS, REFRESH, f"Authorization: Bearer {ACCESS}"]
    wire.replies.extend(
        [
            (401, {"error": "expired"}),
            (
                400,
                {
                    "error": "invalid_grant",
                    "error_description": " | ".join(secrets),
                },
            ),
        ]
        * 3
    )
    h = (
        HarnessBuilder(workspace=tmp_path, model=source().build(MODEL))
        .with_sessions(
            tmp_path / "sessions",
            stats=False,
        )
        .with_config_overrides(titler=None)
        .build()
    )
    with pytest.raises(Exception) as failure:
        await h.run_turn("test auth failure")
    visible = str(failure.value) + str(format_provider_error(failure.value))
    assert "codex login" in visible
    saved = h.session.store.path.read_text()
    dump = tmp_path / ".marim" / "last-provider-error.json"
    persisted = saved + (dump.read_text() if dump.exists() else "")
    for secret in secrets:
        assert secret not in visible
        assert secret not in persisted
    assert not any(
        "openrouter" in req["url"] or "api.openai.com" in req["url"] for req in wire.requests
    )
    await h.aclose()


@pytest.mark.anyio
async def test_subscription_stats_keep_unknown_cost(wire, tmp_path, monkeypatch):
    from marim_harness import usage
    from marim_harness.runtime.builder import HarnessBuilder
    from marim_harness.stats.ledger import StatsLedger

    events = []
    monkeypatch.setattr(StatsLedger, "append", lambda self, event: events.append(event))
    monkeypatch.setattr(usage, "estimate_cost", lambda *args: 42.0)
    h = (
        HarnessBuilder(workspace=tmp_path, model=source().build(MODEL))
        .with_sessions(
            tmp_path / "sessions",
        )
        .with_config_overrides(model_id="openai-codex:gpt-6-astra", titler=None)
        .build()
    )
    wire.replies.append(sse())
    await h.run_turn("hello")
    assert events and events[0].input_tokens == 100
    assert events[0].cost_usd is None and events[0].cost_is_exact is False
    assert events[0].model == "openai-codex:gpt-6-astra"
    assert usage.usage_summary(h.session.usage, h.model_id)["cost_usd"] is None
    await h.aclose()


@pytest.mark.anyio
async def test_subscription_advisor_keeps_mixed_cost_unknown(wire, tmp_path, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from marim_harness.runtime import controller
    from marim_harness.runtime.builder import HarnessBuilder
    from marim_harness.usage import resolve_cost

    preserve = controller.preserve_usage_cost

    def with_upstream_estimate(delta):
        delta.cost = Decimal("42")
        preserve(delta)

    monkeypatch.setattr(controller, "preserve_usage_cost", with_upstream_estimate)

    def executor(messages, info):
        if any(isinstance(part, ToolReturnPart) for msg in messages for part in msg.parts):
            return ModelResponse(parts=[TextPart("consulted")])
        return ModelResponse(parts=[ToolCallPart("advisor", {"prompt": "review"})])

    src = MultiModelSource({"openai-codex": source()}, "openai-codex")
    h = (
        HarnessBuilder(workspace=tmp_path, model=FunctionModel(executor))
        .with_advisor(
            "openai-codex:gpt-6-astra",
        )
        .with_config_overrides(model_source=src, titler=None)
        .build()
    )
    wire.replies.append(sse(text="subscription advice"))
    assert (await h.run_turn("consult")).result == "consulted"
    assert len(wire.requests) == 1
    assert h.session.usage.details.get("estimated_cost_unknown") == 1
    h.session.usage.cost = Decimal("42")
    h.session.usage.details["estimated_cost_micro_usd"] = 42000000
    assert resolve_cost(h.session.usage, "openrouter:anthropic/claude-sonnet-4-6") == (None, False)
    await h.aclose()
