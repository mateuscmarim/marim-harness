"""Fetch a URL and return its content as clean Markdown.

Follows the same pattern as Claude Code's WebFetch tool: fetch HTML via
httpx, convert to Markdown with markdownify, and return the result so the
agent can process it.  Supports HTML pages, plain-text responses, and
redirects (same-host by default).

Safety: only ``http`` / ``https`` URLs are accepted; ``file://``,
``ftp://``, etc. are rejected.  The body is streamed and the read is
capped at ``_MAX_BYTES``; a response that *declares* a size over
``_MAX_DOWNLOAD`` is refused before its body is read.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpcore
import httpx
from markdownify import markdownify as md  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

_TIMEOUT = 30  # seconds
# Read cap: the body is streamed and reading stops here, so we never buffer more
# than this regardless of what the server sends. Since large bodies are offloaded
# to a file (not held in context), this is a processing/disk ceiling, not a
# context one — hence comfortably above the inline limit below.
_MAX_BYTES = 5_000_000  # 5 MB
# Refuse before reading: if the response *declares* (Content-Length) a size over
# this, bail with a clear error rather than streaming a fragment of something
# absurd. Servers that omit Content-Length still get capped by _MAX_BYTES.
_MAX_DOWNLOAD = 25_000_000  # 25 MB
# When an offload directory is available, a result larger than this is written to
# a file and the agent gets a handle + preview instead of the whole body inline
# — so a big page can't flood the turn's context. (read_file/grep can then page
# the file.) ~50k chars ≈ ~12k tokens; small results stay inline, no round-trip.
_INLINE_CHAR_LIMIT = 50_000
_ALLOWED_SCHEMES = frozenset({"http", "https"})
# A leading "<scheme>://" — the only shape we treat as a real scheme. A bare
# "host:port/path" has no "//" after the colon, so it isn't mistaken for a
# "host:" scheme and instead gets https:// assumed (see _normalise_url).
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://")
# How much of an error response body to surface. For API endpoints the useful
# detail (bad key, validation message, rate-limit reason) is in the body, not
# the status line — but we don't want to dump a full HTML error page inline.
_ERROR_BODY_CHARS = 500

# "this host" 0/8 (0.0.0.0 routes to localhost on many stacks) and CGNAT
# 100.64/10 (RFC 6598, carrier-internal) are not public targets — block both
# alongside the usual loopback/private/link-local ranges.
_BLOCKED_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),          # IPv6 unspecified
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _validated_ips(host: str) -> list[str]:
    """Resolve ``host`` and return its IP addresses, refusing if **any** falls in
    a blocked range. Raises ``ValueError`` with a clear message on DNS failure or
    a blocked address.

    The returned list is what callers must connect to: a single DNS lookup whose
    result is both validated *and* used to connect closes the resolve-then-
    reresolve window a DNS-rebinding server could otherwise slip through.
    """
    if not host:
        raise ValueError("empty host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"can't resolve {host!r}: {exc}") from exc
    ips: list[str] = []
    for info in infos:
        bare = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(bare)
        except ValueError:
            continue
        # Normalize IPv4-mapped IPv6 (``::ffff:127.0.0.1``) to its IPv4 form so
        # it's matched against the IPv4 blocks below — otherwise an attacker can
        # spell a blocked v4 address in v6 and slip past every v4 net. ``ipv4_mapped``
        # exists only on IPv6Address (and is None for non-mapped v6), so guard.
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if any(ip in net for net in _BLOCKED_NETS):
            raise ValueError(
                f"refusing to fetch {ip} (private/loopback/link-local address)"
            )
        if bare not in ips:
            ips.append(bare)
    if not ips:
        raise ValueError(f"can't resolve {host!r}: no usable address")
    return ips


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Network backend that resolves+validates each host and connects to the
    exact IP it just validated.

    Validating a hostname and then handing the *name* back to httpcore would let
    it re-resolve at connect time — a DNS-rebinding server can answer with a
    public IP for our check and a private one for the connect. Pinning the
    connect to the address we vetted removes that gap. TLS SNI and certificate
    verification are unaffected: httpcore derives the server hostname from the
    request origin, not from this connect target, so certs still validate against
    the real name. Redirect hops route through here too, so each is vetted the
    same way.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
        self._inner = inner

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        try:
            ip = _validated_ips(host)[0]
        except ValueError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        return await self._inner.connect_tcp(
            ip, port, timeout=timeout, local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args, **kwargs):
        return await self._inner.connect_unix_socket(*args, **kwargs)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _build_client(*, timeout: int) -> httpx.AsyncClient:
    """An ``AsyncClient`` whose DNS is pinned: every connection — the initial one
    and each redirect hop — goes to an address we resolved and validated in the
    same step (see :class:`_PinnedBackend`)."""
    transport = httpx.AsyncHTTPTransport()
    # httpx fully configures the pool (TLS context, limits); we only swap its
    # network backend so the connect target is the validated IP. Reaching into
    # ``_pool`` is the one private-API touch — kept to a single line and pinned
    # by tests so an httpx upgrade that renames it fails loudly.
    transport._pool._network_backend = _PinnedBackend(  # type: ignore[attr-defined]
        transport._pool._network_backend  # type: ignore[attr-defined]
    )
    return httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=True,
        max_redirects=5,
        headers={"User-Agent": _UA},
    )

# Common markers for boilerplate we want to strip from the output.
_BOILERPLATE_SELECTORS = (
    "nav", "footer", "header", "aside",
    "script", "style", "noscript", "iframe",
)

_UA = (
    "Mozilla/5.0 (compatible; marim-harness/1.0; "
    "+https://github.com/marim-dev/marim-harness)"
)


def _normalise_url(url: str) -> str:
    """Strip surrounding whitespace, reject non-HTTP schemes, and assume
    ``https://`` for a bare host.

    A scheme is recognized only when followed by ``://`` — so ``example.com:8080/p``
    is read as a schemeless ``host:port`` (→ ``https://example.com:8080/p``) rather
    than an ``example.com:`` scheme, matching how browsers and Claude Code's
    WebFetch treat a bare host. A genuine non-HTTP scheme (``file:``, ``ftp:``, …)
    is still rejected.
    """
    url = url.strip()
    match = _SCHEME_RE.match(url)
    if match:
        scheme = match.group(1).lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise ValueError(f"Unsupported URL scheme: {scheme!r} (only http/https allowed)")
        return url
    return f"https://{url}"


def _html_to_markdown(html: str) -> str:
    """Convert HTML to Markdown, stripping common boilerplate elements.

    Uses BeautifulSoup's ``decompose()`` to remove non-content blocks
    before conversion so the markdown stays clean.
    """
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]

    soup = BeautifulSoup(html, "html.parser")
    for tag_name in _BOILERPLATE_SELECTORS:
        for el in soup.find_all(tag_name):
            el.decompose()

    cleaned_html = str(soup)
    return md(cleaned_html, heading_style="ATX", strip=["img", "figure"]).strip()


def _json_pretty(text: str) -> str:
    """Pretty-print a JSON document. Pulled out as a module-level function (rather
    than inlined) so it can be offloaded to a worker thread via ``run_sync`` — for a
    multi-megabyte body the parse + re-serialize is CPU-bound and would otherwise
    block the event loop. Raises on invalid JSON (the caller falls back to raw text)."""
    return json.dumps(json.loads(text), indent=2, ensure_ascii=False)


def _title_of(body: str, url: str) -> str:
    """Best-effort one-line title: the first Markdown heading, else the URL."""
    for line in body.splitlines():
        stripped = line.lstrip("#").strip()
        if line.startswith("#") and stripped:
            return stripped
    return url


def _offload(body: str, url: str, offload_dir: Path) -> str:
    """Write *body* to a file under *offload_dir* and return a handle
    (title, source, size, path) plus a short preview, so the agent can
    page the rest with read_file/grep instead of taking the whole body inline."""
    from .offload import _PREVIEW_LINES as _PL
    from .offload import write_preview_file

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    filename = f"fetch-{digest}.md"
    abs_path, preview, n_lines = write_preview_file(
        body, filename=filename, offload_dir=offload_dir)
    return (
        f"# {_title_of(body, url)}\n"
        f"Fetched {url}\n\n"
        f"⚠️ Large page ({len(body):,} chars, {n_lines:,} lines) — full content "
        f"saved to `{abs_path}`. Read more with read_file (it paginates) or grep "
        f"that path for what you need.\n\n"
        f"--- preview (first {min(_PL, n_lines)} lines) ---\n"
        f"{preview}"
    )


def _validate_target(url: str) -> tuple[str | None, str | None]:
    """Normalize *url*, then validate its host resolves to a public address.
    Returns ``(normalized_url, None)`` on success or ``(None, error_message)``
    on any rejection (bad scheme, no host, or an SSRF-blocked address)."""
    try:
        url = _normalise_url(url)
    except ValueError as exc:
        return None, str(exc)

    host = urlparse(url).hostname
    if not host:
        return None, "Fetch failed: URL has no host"
    try:
        # Early, friendly check for the common case. The real enforcement is in
        # the pinned client below, which re-validates atomically per connection
        # (initial + every redirect hop), so this can't be bypassed by rebinding.
        _validated_ips(host)
    except ValueError as exc:
        return None, str(exc)
    return url, None


async def _stream_body(
    url: str, timeout: int
) -> tuple[bytes, str, str | None, bool] | str:
    """Fetch *url*'s body, stopping at the read cap. Returns ``(raw, content_type,
    encoding, truncated)`` on success, or an error string on any failure (a
    declared-oversize body, an HTTP error status, or a transport error) — the
    caller branches on ``isinstance(result, str)``."""
    try:
        async with _build_client(timeout=timeout) as client, \
                client.stream("GET", url) as resp:
            resp.raise_for_status()
            # --- size guard: refuse declared-huge bodies before reading ---
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > _MAX_DOWNLOAD:
                return (
                    f"Fetch aborted: server reports {int(declared):,} bytes, "
                    f"over the {_MAX_DOWNLOAD:,}-byte limit. Try a more specific "
                    f"URL or endpoint."
                )
            content_type = resp.headers.get("content-type", "")
            encoding = resp.encoding
            # --- stream the body, stopping at the read cap ---
            chunks: list[bytes] = []
            total = 0
            truncated = False
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total > _MAX_BYTES:
                    truncated = True
                    break
            raw = b"".join(chunks)[:_MAX_BYTES]
            return raw, content_type, encoding, truncated
    except httpx.HTTPStatusError as exc:
        msg = f"Fetch failed: HTTP {exc.response.status_code} — {exc.response.reason_phrase}"
        # Surface a snippet of the error body: for API endpoints that's where the
        # actionable detail lives. The body wasn't read (we raised on a streamed
        # response), so pull it now — best-effort, so a read/decode failure just
        # leaves the status line.
        try:
            await exc.response.aread()
            detail = exc.response.text.strip()
        except Exception as exc:  # noqa: BLE001 — best-effort; never mask the HTTP error
            logger.debug("best-effort error body read failed", exc_info=True)
            detail = ""
        if detail:
            snippet = detail[:_ERROR_BODY_CHARS]
            if len(detail) > _ERROR_BODY_CHARS:
                snippet += "…"
            msg = f"{msg}\n{snippet}"
        return msg
    except httpx.RequestError as exc:
        return f"Fetch failed: {exc}"


async def _render_body(raw: bytes, content_type: str, encoding: str | None) -> str:
    """Decode *raw* and dispatch to the content-type-appropriate rendering
    (HTML→Markdown, pretty-printed JSON, or plain text passthrough). Decode and
    conversion are CPU-bound, so each is offloaded to a worker thread — the
    httpx streaming that produced *raw* stays async on the loop, but running
    these inline would block it for the whole turn."""
    text = await asyncio.to_thread(raw.decode, encoding or "utf-8", "replace")
    if "html" in content_type or "xhtml" in content_type:
        return await asyncio.to_thread(_html_to_markdown, text)
    if "json" in content_type:
        # Return pretty-printed JSON — the agent can parse it.
        try:
            return await asyncio.to_thread(_json_pretty, text)
        except Exception as exc:
            logger.debug("JSON pretty-print failed, returning raw text: %s", exc, exc_info=True)
            return text
    # Plain text, markdown, SVG, etc.
    return text


async def fetch_url(
    url: str,
    *,
    prompt: str | None = None,
    timeout: int = _TIMEOUT,
    offload_dir: Path | None = None,
) -> str:
    """Fetch *url* and return its body as Markdown.

    *url* must be an HTTP(S) URL.  *prompt* is an optional hint about
    what to extract (included as a header in the output for context but
    does **not** alter the fetch itself).  *timeout* caps the request in
    seconds (default 30).

    If *offload_dir* is given and the body exceeds ``_INLINE_CHAR_LIMIT``,
    the full content is written to a file under that directory and a
    handle + preview is returned instead of the whole body — so a large page
    can't flood the turn's context. Small bodies (and any call without an
    offload directory) are returned inline as before.

    Returns a Markdown-formatted string, or an error message on failure.
    """
    target, error = _validate_target(url)
    if error is not None or target is None:
        return error or "Fetch failed: invalid URL"

    streamed = await _stream_body(target, timeout)
    if isinstance(streamed, str):
        return streamed
    raw, content_type, encoding, truncated = streamed

    # --- decode → markdown/json (CPU-bound; offload off the event loop) ---
    body = await _render_body(raw, content_type, encoding)

    if truncated:
        body += (
            f"\n\n---\n⚠️ Content capped at {_MAX_BYTES:,} bytes — the page was "
            f"larger and the rest was not read."
        )

    # --- optional prompt header ---
    if prompt:
        body = f"## Requested: {prompt}\n\n{body}"

    if not body:
        return "Fetch succeeded but the page was empty."

    # --- offload large bodies so they don't flood context ---
    if offload_dir is not None and len(body) > _INLINE_CHAR_LIMIT:
        return _offload(body, target, offload_dir)

    return body
