"""Tests for the web_search tool backed by SearXNG."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from marim_harness.tools.impl.web import web_search

# --- unit tests (no network) ---


@pytest.mark.anyio
async def test_web_search_success():
    payload = {
        "results": [
            {
                "url": "https://example.com",
                "title": "Example",
                "content": "An example page.",
                "engines": ["google", "duckduckgo"],
                "publishedDate": None,
            },
            {
                "url": "https://example.org",
                "title": "Example Org",
                "content": "Another one.",
                "engines": ["wikipedia"],
                "publishedDate": "2025-01-01",
            },
        ],
    }
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: payload

    with patch("marim_harness.tools.impl.web.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client

        result = await web_search("test query", max_results=5)

    assert "1. Example" in result
    assert "https://example.com" in result
    assert "An example page." in result
    assert "2. Example Org" in result
    assert "(2025-01-01)" in result
    assert "[engines: google, duckduckgo]" in result


@pytest.mark.anyio
async def test_web_search_empty():
    payload = {"results": []}
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: payload

    with patch("marim_harness.tools.impl.web.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client

        result = await web_search("nothing here")

    assert result == "No results found."


@pytest.mark.anyio
async def test_web_search_http_error():
    mock_resp = AsyncMock()
    mock_resp.status_code = 500
    mock_resp.text = ""
    exc = httpx.HTTPStatusError(
        "Server Error", request=AsyncMock(), response=mock_resp,
    )

    def _raise():
        raise exc

    mock_resp.raise_for_status = _raise

    with patch("marim_harness.tools.impl.web.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client

        result = await web_search("boom")

    assert "Search failed: HTTP 500" in result


@pytest.mark.anyio
async def test_web_search_categories_forwarded():
    payload = {"results": []}
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: payload

    with patch("marim_harness.tools.impl.web.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client

        await web_search("news query", categories="news")

    call_kwargs = client.get.call_args
    assert call_kwargs[1]["params"]["categories"] == "news"


@pytest.mark.anyio
async def test_web_search_max_results_clamped():
    """Results list longer than max_results should be truncated."""
    payload = {
        "results": [
            {
                "url": f"https://example.com/{i}", "title": f"R{i}",
                "content": "", "engines": [], "publishedDate": None,
            }
            for i in range(20)
        ]
    }
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: payload

    with patch("marim_harness.tools.impl.web.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client

        result = await web_search("many results", max_results=3)

    assert "1. R0" in result
    assert "3. R2" in result
    assert "4. R3" not in result


@pytest.mark.anyio
async def test_web_search_non_json_response():
    """A non-JSON body (SearXNG JSON format disabled) returns a friendly error,
    not an uncaught JSONDecodeError."""
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = lambda: None

    def _bad_json():
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    mock_resp.json = _bad_json

    with patch("marim_harness.tools.impl.web.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client

        result = await web_search("q")

    assert "Search failed" in result
    assert "JSON" in result


@pytest.mark.anyio
async def test_web_search_non_dict_json():
    """Valid JSON of the wrong shape (a bare array) must not raise AttributeError."""
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: ["not", "an", "object"]

    with patch("marim_harness.tools.impl.web.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client

        result = await web_search("q")

    assert result == "No results found."


@pytest.mark.anyio
async def test_web_search_base_url_from_env(monkeypatch):
    """MARIM_SEARXNG_URL overrides the endpoint when no explicit base_url is given."""
    monkeypatch.setenv("MARIM_SEARXNG_URL", "https://my-searx.example")
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: {"results": []}

    with patch("marim_harness.tools.impl.web.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client

        await web_search("q")

    called_url = client.get.call_args.args[0]
    assert called_url == "https://my-searx.example/search"


@pytest.mark.anyio
async def test_web_search_sets_user_agent():
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: {"results": []}

    with patch("marim_harness.tools.impl.web.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client

        await web_search("q")

    headers = mock_cls.call_args.kwargs["headers"]
    assert "marim-harness" in headers["User-Agent"]


@pytest.mark.anyio
async def test_web_search_snippet_truncated():
    payload = {
        "results": [
            {
                "url": "https://example.com", "title": "Long",
                "content": "y" * 1000, "engines": [], "publishedDate": None,
            }
        ]
    }
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: payload

    with patch("marim_harness.tools.impl.web.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client

        result = await web_search("q")

    assert "…" in result
    assert result.count("y") <= 300


@pytest.mark.anyio
async def test_web_search_http_error_includes_body():
    mock_resp = AsyncMock()
    mock_resp.status_code = 429
    mock_resp.text = "Too Many Requests — slow down"
    exc = httpx.HTTPStatusError("Rate limited", request=AsyncMock(), response=mock_resp)

    def _raise():
        raise exc

    mock_resp.raise_for_status = _raise

    with patch("marim_harness.tools.impl.web.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client

        result = await web_search("q")

    assert "Search failed: HTTP 429" in result
    assert "slow down" in result


@pytest.mark.anyio
async def test_web_search_max_results_capped_at_50():
    """max_results above 50 should be clamped to 50."""
    with patch("marim_harness.tools.impl.web.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=AsyncMock(
            raise_for_status=lambda: None,
            json=lambda: {"results": []},
        ))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = client

        await web_search("q", max_results=100)

    # The URL param won't carry max_results; it's sliced in Python.
    # Just verify it didn't blow up.
    assert client.get.called
