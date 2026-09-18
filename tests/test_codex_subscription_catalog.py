"""Native subscription discovery reaches every picker without a CLI or model turn."""

import httpx
import httpx2
import pytest

from marim_harness.config import codex_subscription_catalog as catalog
from marim_harness.config.model import ModelConfig, ModelSource, MultiModelSource
from marim_harness.workspace.catalog import ModelEntry
from tests._codex_subscription import ACCESS, REFRESH, auth_path, source
from tests._codex_subscription import wire as wire  # noqa: F401
from tests.test_codex_subscription_surfaces import AUTH, server

pytestmark = pytest.mark.anyio


async def test_mobile_lists_visible_subscription_models(wire, tmp_path, monkeypatch):
    wire.catalog_payload = {
        "models": [
            {"slug": "gpt-6-astra", "display_name": "Astra", "visibility": "list"},
            {"slug": "gpt-5.6-terra", "display_name": "Terra", "visibility": "list"},
            {"slug": "gpt-5.6-luna", "display_name": "Luna", "visibility": "list"},
            {"slug": "hidden-review", "visibility": "hide"},
        ]
    }
    native = ModelSource(ModelConfig(provider="openai-codex", model="gpt-5.6-terra"))
    src = MultiModelSource({"openai-codex": native}, "openai-codex")
    monkeypatch.setattr(MultiModelSource, "from_env", lambda: src)
    app, _, _ = server(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/models")).status_code == 401
        assert not wire.catalog_requests
        response = await client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["models"] == [
        {"id": f"openai-codex:{slug}", "provider": "openai-codex", "name": name}
        for slug, name in (
            ("gpt-6-astra", "Astra"),
            ("gpt-5.6-terra", "Terra"),
            ("gpt-5.6-luna", "Luna"),
        )
    ]
    assert native.cfg.model == "gpt-5.6-terra"
    assert len(wire.catalog_requests) == 1
    assert wire.catalog_requests[0]["headers"]["authorization"] == f"Bearer {ACCESS}"
    assert not wire.requests  # Listing never generates tokens.


def test_catalog_metadata_and_malformed_rows():
    entries = catalog.parse_subscription_models(
        {
            "models": [
                {
                    "slug": "vision",
                    "display_name": "Vision",
                    "visibility": "list",
                    "input_modalities": ["text", "image"],
                    "context_window": 272000,
                    "supported_reasoning_levels": [{"effort": "low"}],
                },
                {"slug": "vision", "display_name": "Duplicate", "visibility": "list"},
                {
                    "slug": "text",
                    "visibility": "list",
                    "input_modalities": ["text"],
                    "supported_reasoning_levels": [],
                    "context_window": True,
                },
                {"slug": "unknown", "visibility": "list", "display_name": 12},
                {"slug": "hidden", "visibility": "hide"},
                {"slug": "no-visibility"},
                {"slug": 1, "visibility": "list"},
                {"slug": " ", "visibility": "list"},
                None,
            ]
        }
    )
    assert entries == [
        ModelEntry(
            "vision", "Vision", supports_images=True, supports_thinking=True, context_window=272000
        ),
        ModelEntry("text", "text", supports_images=False, supports_thinking=False),
        ModelEntry("unknown", "unknown"),
    ]


async def test_discovers_without_configured_default_and_ignores_api_overrides(wire, monkeypatch):
    for key in ("OPENAI_API_KEY", "MARIM_API_KEY", "MARIM_BASE_URL"):
        monkeypatch.setenv(key, "https://forbidden.invalid/paid-secret")
    wire.catalog_payload = {"models": [{"slug": "discovered", "visibility": "list"}]}
    src = ModelSource(ModelConfig(provider="openai-codex", model=None))
    assert await src.list_models(strict=True) == [ModelEntry("discovered", "discovered")]
    request = wire.catalog_requests[0]
    assert request["url"] == ("https://chatgpt.com/backend-api/codex/models?client_version=0.154.0")
    assert request["headers"]["chatgpt-account-id"] == "fixture-account"
    assert request["body"] == ""
    assert not wire.requests


@pytest.mark.parametrize("payload", [None, {}, {"models": None}, {"models": "bad"}])
async def test_malformed_response_falls_back_but_strict_raises(wire, payload):
    wire.catalog_payload = payload
    src = source()
    assert await src.list_models() == [ModelEntry(src.cfg.model, src.cfg.model)]
    with pytest.raises(ValueError, match="models list"):
        await src.list_models(strict=True)


async def test_empty_catalog_and_missing_credentials(wire):
    src = source()
    assert await src.list_models(strict=True) == []
    auth_path().unlink()
    before = len(wire.catalog_requests)
    assert await src.list_models() == [ModelEntry(src.cfg.model, src.cfg.model)]
    from pydantic_ai.exceptions import UserError

    with pytest.raises(UserError, match="codex login"):
        await src.list_models(strict=True)
    assert len(wire.catalog_requests) == before
    assert await catalog.list_subscription_models() == []


@pytest.mark.parametrize("failure", ["timeout", "status", "cancel"])
async def test_client_cleanup_and_safe_failure_logs(wire, monkeypatch, caplog, failure):
    import asyncio

    async def fail(request):
        if failure == "timeout":
            raise httpx2.ReadTimeout("fixture-secret")
        if failure == "cancel":
            raise asyncio.CancelledError()
        return httpx2.Response(503, json={"secret": "fixture-secret"})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(fail))
    monkeypatch.setattr(catalog, "AsyncClient", lambda **kwargs: client)
    with caplog.at_level("DEBUG", logger=catalog.__name__):
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await source().list_models()
        else:
            assert await source().list_models()
            assert "catalog unavailable" in caplog.text
    assert client.is_closed
    assert "fixture-secret" not in caplog.text


async def test_catalog_401_refresh_owned_by_upstream_and_auth_file_unchanged(wire):
    original = auth_path().read_bytes()

    async def models(request):
        if len(wire.catalog_requests) == 1:
            return httpx2.Response(401)
        return httpx2.Response(200, json={"models": [{"slug": "fresh", "visibility": "list"}]})

    wire.catalog_handler = models
    wire.replies.append((200, {"access_token": "fresh-token", "refresh_token": "fresh-refresh"}))
    assert await source().list_models(strict=True) == [ModelEntry("fresh", "fresh")]
    assert len(wire.refreshes) == 1
    assert REFRESH in wire.refreshes[0]["body"]
    assert wire.catalog_requests[-1]["headers"]["authorization"] == "Bearer fresh-token"
    assert auth_path().read_bytes() == original
    assert not wire.requests
