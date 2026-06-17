"""Tests for the fetch_url tool (URL → Markdown)."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from marim_harness.tools.fetch import fetch_url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import json as _json


def _mock_response(
    *,
    text: str = "",
    content_type: str = "text/html; charset=utf-8",
    status_code: int = 200,
    raise_for_status_error: bool = False,
) -> AsyncMock:
    """Build a fake httpx.Response."""
    resp = AsyncMock()
    resp.status_code = status_code
    resp.headers = {"content-type": content_type}
    resp.content = text.encode("utf-8")
    resp.encoding = "utf-8"

    # raise_for_status must be a *regular* callable — httpx calls it
    # synchronously, not as a coroutine.
    if raise_for_status_error:
        exc = httpx.HTTPStatusError(
            "Error",
            request=AsyncMock(),
            response=resp,
        )
        def _raise():
            raise exc
        resp.raise_for_status = _raise
    else:
        resp.raise_for_status = lambda: None  # type: ignore[assignment]

    return resp


def _patch_client(mock_resp: AsyncMock) -> "patch":
    """Context-manager patch for ``httpx.AsyncClient`` returning *mock_resp*."""
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return patch("marim_harness.tools.fetch.httpx.AsyncClient", return_value=client)


# ---------------------------------------------------------------------------
# Tests — successful fetches
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_html_converts_to_markdown():
    html = "<html><body><h1>Hello</h1><p>This is <b>bold</b> content.</p></body></html>"
    resp = _mock_response(text=html, content_type="text/html")

    with _patch_client(resp) as mock_cls:
        result = await fetch_url("https://example.com")

    assert "# Hello" in result
    assert "bold" in result
    assert "<h1>" not in result  # raw HTML should not appear


@pytest.mark.anyio
async def test_fetch_plain_text_returned_as_is():
    text = "Just plain text, no HTML."
    resp = _mock_response(text=text, content_type="text/plain")

    with _patch_client(resp):
        result = await fetch_url("https://example.com/data.txt")

    assert result == text


@pytest.mark.anyio
async def test_fetch_json_pretty_printed():
    import json

    data = {"key": "value", "nested": [1, 2, 3]}
    resp = _mock_response(text=json.dumps(data), content_type="application/json")
    resp.json = lambda: data  # synchronous — httpx.Response.json() is sync

    with _patch_client(resp):
        result = await fetch_url("https://api.example.com/data")

    assert '"key": "value"' in result
    assert '"nested"' in result
    # Pretty-printed = has indentation
    assert "  " in result


@pytest.mark.anyio
async def test_fetch_json_with_charset():
    import json

    data = {"hello": "world"}
    resp = _mock_response(text=json.dumps(data), content_type="application/json; charset=utf-8")
    resp.json = lambda: data  # synchronous

    with _patch_client(resp):
        result = await fetch_url("https://api.example.com/data")

    assert '"hello": "world"' in result


@pytest.mark.anyio
async def test_fetch_html_strips_nav_and_script():
    html = """
    <html><body>
    <nav><a href="/">Home</a></nav>
    <main><h1>Article</h1><p>The good stuff.</p></main>
    <script>analytics.track();</script>
    <footer>Copyright 2025</footer>
    </body></html>
    """
    resp = _mock_response(text=html, content_type="text/html")

    with _patch_client(resp):
        result = await fetch_url("https://example.com/article")

    assert "Article" in result
    assert "The good stuff" in result
    assert "analytics.track" not in result
    assert "Copyright" not in result
    assert "Home" not in result


@pytest.mark.anyio
async def test_fetch_prompt_header_included():
    html = "<html><body><p>Content</p></body></html>"
    resp = _mock_response(text=html, content_type="text/html")

    with _patch_client(resp):
        result = await fetch_url("https://example.com", prompt="Find pricing info")

    assert "## Requested: Find pricing info" in result
    assert "Content" in result


@pytest.mark.anyio
async def test_fetch_no_prompt_no_header():
    html = "<html><body><p>Content</p></body></html>"
    resp = _mock_response(text=html, content_type="text/html")

    with _patch_client(resp):
        result = await fetch_url("https://example.com")

    assert "## Requested" not in result
    assert "Content" in result


# ---------------------------------------------------------------------------
# Tests — error handling
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_http_error():
    resp = _mock_response(status_code=404, raise_for_status_error=True)

    with _patch_client(resp):
        result = await fetch_url("https://example.com/missing")

    assert "Fetch failed: HTTP 404" in result


@pytest.mark.anyio
async def test_fetch_server_error():
    resp = _mock_response(status_code=500, raise_for_status_error=True)

    with _patch_client(resp):
        result = await fetch_url("https://example.com/boom")

    assert "Fetch failed: HTTP 500" in result


@pytest.mark.anyio
async def test_fetch_connection_error():
    async with AsyncMock() as client:
        client.get.side_effect = httpx.ConnectError("Connection refused")
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("marim_harness.tools.fetch.httpx.AsyncClient", return_value=client):
            result = await fetch_url("https://unreachable.example.com")

    assert "Fetch failed:" in result
    assert "Connection refused" in result


@pytest.mark.anyio
async def test_fetch_rejects_file_scheme():
    result = await fetch_url("file:///etc/passwd")
    assert "Unsupported URL scheme" in result


@pytest.mark.anyio
async def test_fetch_rejects_ftp_scheme():
    result = await fetch_url("ftp://files.example.com/pub")
    assert "Unsupported URL scheme" in result


# ---------------------------------------------------------------------------
# Tests — truncation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_truncates_large_page():
    """Pages over _MAX_BYTES should be truncated with a note."""
    import marim_harness.tools.fetch as mod

    big_html = "<html><body>" + "x" * 1_100_000 + "</body></html>"
    resp = _mock_response(text=big_html, content_type="text/html")

    with _patch_client(resp):
        result = await fetch_url("https://example.com/big")

    assert "truncated" in result.lower()
    assert "bytes" in result.lower()


# ---------------------------------------------------------------------------
# Tests — redirects
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_follows_redirects():
    resp = _mock_response(text="<p>Final page</p>", content_type="text/html")

    with _patch_client(resp) as mock_cls:
        result = await fetch_url("https://example.com/old")

    # Just verify it succeeded — httpx handles redirects internally
    assert "Final page" in result


# ---------------------------------------------------------------------------
# Tests — empty page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_empty_page():
    resp = _mock_response(text="", content_type="text/html")

    with _patch_client(resp):
        result = await fetch_url("https://example.com/empty")

    assert "empty" in result.lower()


# ---------------------------------------------------------------------------
# Tests — offload-to-file for large pages (when a workspace_root is given)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_small_page_with_workspace_returned_inline(tmp_path):
    """A small page is still returned inline even when a workspace is available —
    no file round-trip for the common case."""
    html = "<html><body><h1>Small</h1><p>Just a little content.</p></body></html>"
    resp = _mock_response(text=html, content_type="text/html")

    with _patch_client(resp):
        result = await fetch_url("https://example.com/small", workspace_root=tmp_path)

    assert "# Small" in result
    assert "Just a little content." in result
    # Nothing offloaded.
    assert not (tmp_path / ".marim" / "fetch").exists()


@pytest.mark.anyio
async def test_fetch_large_page_offloaded_to_file(tmp_path):
    """A large page is written to a gitignored workspace file; the tool returns a
    handle + preview (so read_file/grep can page through) rather than flooding
    context with the whole body."""
    paras = "".join(
        f"<p>Paragraph number {i} with several words here.</p>" for i in range(4000)
    )
    html = f"<html><body><h1>Big Doc</h1>{paras}</body></html>"
    resp = _mock_response(text=html, content_type="text/html")

    with _patch_client(resp):
        result = await fetch_url("https://example.com/big-doc", workspace_root=tmp_path)

    # The handle points at a workspace-relative path under .marim/fetch/.
    assert ".marim/fetch/" in result
    # Preview shows the start...
    assert "Paragraph number 0 " in result
    # ...but NOT the whole body (last paragraph must not be inline).
    assert "Paragraph number 3999" not in result

    # The full content lives on disk and is readable.
    files = list((tmp_path / ".marim" / "fetch").glob("*.md"))
    assert len(files) == 1
    full = files[0].read_text()
    assert "Paragraph number 0 " in full
    assert "Paragraph number 3999" in full


@pytest.mark.anyio
async def test_fetch_offload_path_is_workspace_relative(tmp_path):
    """The path in the handle must be relative to the workspace root so the agent
    can hand it straight to read_file/grep (which are workspace-sandboxed)."""
    paras = "".join(
        f"<p>Filler paragraph {i} with enough text to grow.</p>" for i in range(4000)
    )
    html = f"<html><body>{paras}</body></html>"
    resp = _mock_response(text=html, content_type="text/html")

    with _patch_client(resp):
        result = await fetch_url("https://example.com/big", workspace_root=tmp_path)

    files = list((tmp_path / ".marim" / "fetch").glob("*.md"))
    rel = files[0].relative_to(tmp_path).as_posix()
    assert rel in result
    assert str(tmp_path) not in result  # no absolute path leaks
