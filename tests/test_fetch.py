"""Tests for the fetch_url tool (URL → Markdown)."""

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from marim_harness.tools.impl.fetch import fetch_url

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(
    *,
    text: str = "",
    content_type: str = "text/html; charset=utf-8",
    status_code: int = 200,
    raise_for_status_error: bool = False,
    content_length: "int | None" = None,
    reason_phrase: str = "",
    error_body: str = "",
) -> AsyncMock:
    """Build a fake streamed httpx.Response. ``fetch_url`` reads the body via
    ``aiter_bytes()`` under ``client.stream(...)``, so the body is yielded in
    chunks. ``content_length`` sets the declared header used by the size guard.
    ``error_body`` is what the HTTP-error handler reads via ``aread()``/``.text``."""
    resp = AsyncMock()
    resp.status_code = status_code
    resp.reason_phrase = reason_phrase
    # The error handler does ``await resp.aread()`` then reads ``resp.text``; keep
    # ``.text`` a real str (AsyncMock would otherwise hand back a Mock).
    resp.text = error_body
    headers = {"content-type": content_type}
    if content_length is not None:
        headers["content-length"] = str(content_length)
    resp.headers = headers
    resp.encoding = "utf-8"
    data = text.encode("utf-8")

    async def _aiter_bytes(chunk_size: int = 65536):
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    resp.aiter_bytes = _aiter_bytes

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
    """Patch ``httpx.AsyncClient`` so ``client.stream("GET", url)`` yields
    *mock_resp* as an async context manager (matching how ``fetch_url`` reads)."""
    stream_cm = AsyncMock()
    stream_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    client = AsyncMock()
    client.stream = MagicMock(return_value=stream_cm)  # stream() is a sync call
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return patch("marim_harness.tools.impl.fetch.httpx.AsyncClient", return_value=client)


# ---------------------------------------------------------------------------
# Tests — successful fetches
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_html_converts_to_markdown():
    html = "<html><body><h1>Hello</h1><p>This is <b>bold</b> content.</p></body></html>"
    resp = _mock_response(text=html, content_type="text/html")

    with _patch_client(resp):
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
        result = await fetch_url("https://198.51.100.1/data")

    assert '"key": "value"' in result
    assert '"nested"' in result
    # Pretty-printed = has indentation
    assert "  " in result


@pytest.mark.anyio
async def test_fetch_invalid_json_falls_back_to_raw_text():
    """A response declaring application/json but shipping a malformed body must
    not crash the tool — _json_pretty raises, and fetch_url falls back to the
    raw decoded text rather than propagating the JSON decode error."""
    resp = _mock_response(text="not valid json {{{", content_type="application/json")

    with _patch_client(resp):
        result = await fetch_url("https://example.com/bad.json")

    assert result == "not valid json {{{"


@pytest.mark.anyio
async def test_fetch_json_with_charset():
    import json

    data = {"hello": "world"}
    resp = _mock_response(text=json.dumps(data), content_type="application/json; charset=utf-8")
    resp.json = lambda: data  # synchronous

    with _patch_client(resp):
        result = await fetch_url("https://198.51.100.1/data")

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
async def test_fetch_http_error_includes_body_snippet():
    resp = _mock_response(
        status_code=401,
        reason_phrase="Unauthorized",
        raise_for_status_error=True,
        error_body='{"error": "invalid api key"}',
    )

    with _patch_client(resp):
        result = await fetch_url("https://198.51.100.5/v1/thing")

    assert "Fetch failed: HTTP 401 — Unauthorized" in result
    assert "invalid api key" in result


@pytest.mark.anyio
async def test_fetch_http_error_body_truncated():
    resp = _mock_response(
        status_code=500,
        raise_for_status_error=True,
        error_body="x" * 5000,
    )

    with _patch_client(resp):
        result = await fetch_url("https://198.51.100.5/boom")

    # Body snippet is capped (500 chars) and ellipsized — never the full 5k.
    assert "…" in result
    assert result.count("x") <= 500


@pytest.mark.anyio
async def test_fetch_http_error_without_body_is_just_status():
    resp = _mock_response(status_code=404, raise_for_status_error=True)

    with _patch_client(resp):
        result = await fetch_url("https://example.com/missing")

    assert "Fetch failed: HTTP 404" in result
    assert "\n" not in result.strip()  # no body snippet appended on a second line


@pytest.mark.anyio
async def test_fetch_connection_error():
    client = AsyncMock()
    client.stream = MagicMock(side_effect=httpx.ConnectError("Connection refused"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("marim_harness.tools.impl.fetch.httpx.AsyncClient", return_value=client):
        result = await fetch_url("https://198.51.100.1/")

    assert "Fetch failed:" in result
    assert "Connection refused" in result


@pytest.mark.anyio
async def test_fetch_rejects_url_with_no_host():
    """A scheme with an empty authority (``https:///path``) parses to a URL with
    no hostname — must be rejected with a clear message, not crash downstream
    on a ``None`` host."""
    result = await fetch_url("https:///path")
    assert result == "Fetch failed: URL has no host"


@pytest.mark.anyio
async def test_fetch_http_error_body_read_failure_falls_back_to_status_only():
    """If reading the error response body itself raises (network hiccup mid-read,
    a broken transport), the best-effort read must not mask the real HTTP error —
    the status line still comes back, just without a body snippet."""
    resp = _mock_response(status_code=503, reason_phrase="Service Unavailable",
                          raise_for_status_error=True)
    resp.aread = AsyncMock(side_effect=RuntimeError("boom"))

    with _patch_client(resp):
        result = await fetch_url("https://example.com/flaky")

    assert result == "Fetch failed: HTTP 503 — Service Unavailable"


@pytest.mark.anyio
async def test_fetch_rejects_file_scheme():
    result = await fetch_url("file:///etc/passwd")
    assert "Unsupported URL scheme" in result


@pytest.mark.anyio
async def test_fetch_rejects_ftp_scheme():
    result = await fetch_url("ftp://files.example.com/pub")
    assert "Unsupported URL scheme" in result


@pytest.mark.anyio
async def test_fetch_schemeless_url_assumes_https():
    resp = _mock_response(text="ok", content_type="text/plain")

    with _patch_client(resp) as client_cls:
        result = await fetch_url("198.51.100.7/path")

    assert result == "ok"
    # https:// is prepended before the request is made.
    called_url = client_cls.return_value.stream.call_args.args[1]
    assert called_url == "https://198.51.100.7/path"


@pytest.mark.anyio
async def test_fetch_schemeless_host_port_not_mistaken_for_scheme():
    resp = _mock_response(text="ok", content_type="text/plain")

    with _patch_client(resp) as client_cls:
        result = await fetch_url("198.51.100.7:8080/path")

    assert result == "ok"
    called_url = client_cls.return_value.stream.call_args.args[1]
    assert called_url == "https://198.51.100.7:8080/path"


# ---------------------------------------------------------------------------
# Tests — read cap (_MAX_BYTES) and the download size guard (_MAX_DOWNLOAD)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_caps_read_at_max_bytes():
    """A response larger than _MAX_BYTES is read up to the cap and flagged — the
    stream is stopped, not the whole body buffered."""
    from marim_harness.tools.impl.fetch import _MAX_BYTES

    big = "x" * (_MAX_BYTES + 500_000)
    resp = _mock_response(text=big, content_type="text/plain")

    with _patch_client(resp):
        result = await fetch_url("https://example.com/big")

    assert "capped" in result.lower()
    # Body must not exceed the cap (plus the short note).
    assert len(result) <= _MAX_BYTES + 500


@pytest.mark.anyio
async def test_fetch_aborts_when_content_length_exceeds_limit(tmp_path):
    """If the server *declares* a size over _MAX_DOWNLOAD, bail before reading
    the body — return a clear error, write nothing, offload nothing."""
    from marim_harness.tools.impl.fetch import _MAX_DOWNLOAD

    resp = _mock_response(
        text="ignored — we never read this",
        content_type="text/plain",
        content_length=_MAX_DOWNLOAD + 1,
    )

    with _patch_client(resp):
        result = await fetch_url("https://example.com/huge", workspace_root=tmp_path)

    assert "aborted" in result.lower()
    assert "ignored" not in result  # body was never read
    assert not (tmp_path / ".marim" / "fetch").exists()


@pytest.mark.anyio
async def test_fetch_within_content_length_limit_is_read():
    """A declared size under the limit is fetched normally."""
    resp = _mock_response(
        text="<p>Fine</p>", content_type="text/html", content_length=11
    )

    with _patch_client(resp):
        result = await fetch_url("https://example.com/ok")

    assert "Fine" in result


# ---------------------------------------------------------------------------
# Tests — redirects
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_follows_redirects():
    resp = _mock_response(text="<p>Final page</p>", content_type="text/html")

    with _patch_client(resp):
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
async def test_fetch_offload_handle_has_title_and_saved_path(tmp_path, monkeypatch):
    from marim_harness.tools.impl import fetch
    monkeypatch.setattr(fetch, "_INLINE_CHAR_LIMIT", 20)
    body = "# My Title\n" + "\n".join(f"para {i}" for i in range(50))
    out = fetch._offload(body, "https://example.com/x", tmp_path)
    assert out.startswith("# My Title")
    assert "Fetched https://example.com/x" in out
    assert "saved to" in out
    saved = list((tmp_path / ".marim" / "fetch").glob("*.md"))
    assert len(saved) == 1 and saved[0].read_text() == body


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


# ---------------------------------------------------------------------------
# Tests — SSRF: refuse private/loopback/link-local addresses
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_refuses_loopback_ipv4(monkeypatch):
    """fetch_url must refuse to fetch 127.0.0.1 — local services are not a target."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *_: [(socket.AF_INET, socket.SOCK_STREAM,
                                          0, "", (host, 0))])
    result = await fetch_url("http://127.0.0.1/admin")
    assert "127.0.0.1" in result or "private" in result.lower() or "loopback" in result.lower()
    assert "Fetched" not in result


@pytest.mark.anyio
async def test_fetch_refuses_private_rfc1918(monkeypatch):
    """RFC1918 ranges (10/8, 172.16/12, 192.168/16) are blocked too."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *_: [(socket.AF_INET, socket.SOCK_STREAM,
                                          0, "", (host, 0))])
    result = await fetch_url("http://10.0.0.5/internal")
    assert "Fetched" not in result


@pytest.mark.anyio
async def test_fetch_refuses_link_local_metadata(monkeypatch):
    """AWS/GCP instance metadata lives at 169.254.169.254 — must be blocked."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *_: [(socket.AF_INET, socket.SOCK_STREAM,
                                          0, "", (host, 0))])
    result = await fetch_url("http://169.254.169.254/latest/meta-data/")
    assert "Fetched" not in result


@pytest.mark.anyio
async def test_fetch_refuses_ipv6_loopback(monkeypatch):
    """[::1] must be blocked just like 127.0.0.1."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *_: [(socket.AF_INET6, socket.SOCK_STREAM,
                                          0, "", (host, 0, 0, 0))])
    result = await fetch_url("http://[::1]/admin")
    assert "Fetched" not in result


def _getaddrinfo_returning(addr: str, *, family: int = socket.AF_INET):
    """A fake getaddrinfo that resolves any host to a single crafted address,
    so an SSRF check can be driven against an attacker-chosen IP."""
    sockaddr = (addr, 0) if family == socket.AF_INET else (addr, 0, 0, 0)
    return lambda host, *_: [(family, socket.SOCK_STREAM, 0, "", sockaddr)]


@pytest.mark.anyio
async def test_fetch_refuses_ipv4_mapped_loopback(monkeypatch):
    """``::ffff:127.0.0.1`` is loopback spelled in IPv4-mapped IPv6 form — it must
    be normalized to its v4 address and refused, not waved through the v6 nets."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        _getaddrinfo_returning("::ffff:127.0.0.1", family=socket.AF_INET6))
    result = await fetch_url("http://mapped-loopback.example/admin")
    assert "Fetched" not in result


@pytest.mark.anyio
async def test_fetch_refuses_ipv4_mapped_private(monkeypatch):
    """``::ffff:10.0.0.1`` maps to RFC1918 10/8 — refuse after normalization."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        _getaddrinfo_returning("::ffff:10.0.0.1", family=socket.AF_INET6))
    result = await fetch_url("http://mapped-private.example/internal")
    assert "Fetched" not in result


@pytest.mark.anyio
async def test_fetch_refuses_unspecified_ipv4(monkeypatch):
    """0.0.0.0 routes to localhost on many stacks — block it (and the 0/8 net)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("0.0.0.0"))
    result = await fetch_url("http://zero.example/")
    assert "Fetched" not in result


@pytest.mark.anyio
async def test_fetch_refuses_unspecified_ipv6(monkeypatch):
    """:: (IPv6 unspecified) must be refused."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        _getaddrinfo_returning("::", family=socket.AF_INET6))
    result = await fetch_url("http://zero6.example/")
    assert "Fetched" not in result


@pytest.mark.anyio
async def test_fetch_refuses_cgnat(monkeypatch):
    """CGNAT 100.64.0.0/10 (RFC 6598) is carrier-internal — not a public target."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("100.64.1.2"))
    result = await fetch_url("http://cgnat.example/")
    assert "Fetched" not in result


@pytest.mark.anyio
async def test_fetch_allows_public_ipv4(monkeypatch):
    """Prove the new blocks don't over-block: a normal public IP still fetches."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("93.184.216.34"))
    resp = _mock_response(text="<p>Public OK</p>", content_type="text/html")
    with _patch_client(resp):
        result = await fetch_url("http://public.example/")
    assert "Public OK" in result


# ---------------------------------------------------------------------------
# Tests — SSRF: connection is pinned to the validated IP (no DNS-rebinding gap)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pinned_backend_connects_to_validated_ip(monkeypatch):
    """The backend must hand the resolved IP to the inner connect, not the
    hostname — otherwise httpcore would re-resolve and a rebinding server could
    swap in a private address between our check and the connect."""
    from marim_harness.tools.impl.fetch import _PinnedBackend

    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *_: [(socket.AF_INET, socket.SOCK_STREAM,
                                          0, "", ("93.184.216.34", 0))])
    inner = AsyncMock()
    inner.connect_tcp = AsyncMock(return_value="stream")
    backend = _PinnedBackend(inner)

    out = await backend.connect_tcp("example.com", 443)

    assert out == "stream"
    # Connected to the validated IP, never the hostname.
    called_host = inner.connect_tcp.call_args.args[0]
    assert called_host == "93.184.216.34"
    assert called_host != "example.com"


@pytest.mark.anyio
async def test_pinned_backend_refuses_private_ip(monkeypatch):
    """A host resolving to a blocked range raises a ConnectError at connect time
    (covers redirect hops, which never hit the early pre-check)."""
    import httpcore

    from marim_harness.tools.impl.fetch import _PinnedBackend

    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *_: [(socket.AF_INET, socket.SOCK_STREAM,
                                          0, "", ("10.0.0.5", 0))])
    backend = _PinnedBackend(AsyncMock())

    with pytest.raises(httpcore.ConnectError) as ei:
        await backend.connect_tcp("internal.example.com", 80)
    assert "10.0.0.5" in str(ei.value) or "private" in str(ei.value).lower()


def test_validated_ips_returns_public_addresses(monkeypatch):
    from marim_harness.tools.impl.fetch import _validated_ips

    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *_: [(socket.AF_INET, socket.SOCK_STREAM,
                                          0, "", ("93.184.216.34", 0))])
    assert _validated_ips("example.com") == ["93.184.216.34"]


def test_validated_ips_raises_when_any_address_blocked(monkeypatch):
    """If a name resolves to a mix of public and private, refuse outright."""
    from marim_harness.tools.impl.fetch import _validated_ips

    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *_: [
                            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
                            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
                        ])
    with pytest.raises(ValueError, match="private/loopback/link-local"):
        _validated_ips("rebind.example.com")
