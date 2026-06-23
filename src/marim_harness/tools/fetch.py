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

import hashlib
import ipaddress
import logging
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

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
# When a workspace is available, a result larger than this is written to a file
# and the agent gets a handle + preview instead of the whole body inline — so a
# big page can't flood the turn's context. (read_file/grep can then page the
# file.) ~50k chars ≈ ~12k tokens; small results stay inline, no round-trip.
_INLINE_CHAR_LIMIT = 50_000
# Where offloaded fetch bodies live, relative to the workspace root. Gitignored.
_FETCH_DIR = (".marim", "fetch")
_ALLOWED_SCHEMES = frozenset({"http", "https"})

_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _resolve_safely(host: str) -> list[str]:
    """Resolve ``host`` and refuse if any address falls in a blocked range.
    Returns the resolved address list on success; raises ``ValueError`` with a
    clear message on DNS failure or any blocked address.
    """
    if not host:
        raise ValueError("empty host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"can't resolve {host!r}: {exc}") from exc
    addrs: set[str] = {str(i[4][0]) for i in infos}
    for raw in addrs:
        bare = raw.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(bare)
        except ValueError:
            continue
        if any(ip in net for net in _BLOCKED_NETS):
            raise ValueError(
                f"refusing to fetch {ip} (private/loopback/link-local address)"
            )
    return sorted(addrs)

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
    """Strip trailing whitespace and reject non-HTTP schemes."""
    url = url.strip()
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme and scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Unsupported URL scheme: {scheme!r} (only http/https allowed)")
    return url


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


def _title_of(body: str, url: str) -> str:
    """Best-effort one-line title: the first Markdown heading, else the URL."""
    for line in body.splitlines():
        stripped = line.lstrip("#").strip()
        if line.startswith("#") and stripped:
            return stripped
    return url


def _offload(body: str, url: str, workspace_root: Path) -> str:
    """Write *body* to a gitignored file under the workspace and return a handle
    (title, source, size, relative path) plus a short preview, so the agent can
    page the rest with read_file/grep instead of taking the whole body inline."""
    from .offload import _PREVIEW_LINES as _PL
    from .offload import write_preview_file

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    rel = Path(*_FETCH_DIR, f"{digest}.md")
    rel_posix, preview, n_lines = write_preview_file(body, rel=rel,
                                                     workspace_root=workspace_root)
    return (
        f"# {_title_of(body, url)}\n"
        f"Fetched {url}\n\n"
        f"⚠️ Large page ({len(body):,} chars, {n_lines:,} lines) — full content "
        f"saved to `{rel_posix}`. Read more with read_file (it paginates) or grep "
        f"that path for what you need.\n\n"
        f"--- preview (first {min(_PL, n_lines)} lines) ---\n"
        f"{preview}"
    )


async def fetch_url(
    url: str,
    *,
    prompt: Optional[str] = None,
    timeout: int = _TIMEOUT,
    workspace_root: Optional[Path] = None,
) -> str:
    """Fetch *url* and return its body as Markdown.

    *url* must be an HTTP(S) URL.  *prompt* is an optional hint about
    what to extract (included as a header in the output for context but
    does **not** alter the fetch itself).  *timeout* caps the request in
    seconds (default 30).

    If *workspace_root* is given and the body exceeds ``_INLINE_CHAR_LIMIT``,
    the full content is written to a gitignored file under the workspace and a
    handle + preview is returned instead of the whole body — so a large page
    can't flood the turn's context. Small bodies (and any call without a
    workspace) are returned inline as before.

    Returns a Markdown-formatted string, or an error message on failure.
    """
    try:
        url = _normalise_url(url)
    except ValueError as exc:
        return str(exc)

    host = urlparse(url).hostname
    if not host:
        return "Fetch failed: URL has no host"
    try:
        _resolve_safely(host)
    except ValueError as exc:
        return str(exc)

    async def _check_redirect(request):
        h = urlparse(str(request.url)).hostname or ""
        try:
            _resolve_safely(h)
        except ValueError as exc:
            raise httpx.RequestError(str(exc), request=request) from exc

    truncated = False
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": _UA},
            event_hooks={"request": [_check_redirect]},
        ) as client:
            async with client.stream("GET", url) as resp:
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
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        truncated = True
                        break
                raw = b"".join(chunks)[:_MAX_BYTES]
    except httpx.HTTPStatusError as exc:
        return f"Fetch failed: HTTP {exc.response.status_code} — {exc.response.reason_phrase}"
    except httpx.RequestError as exc:
        return f"Fetch failed: {exc}"

    # --- decode ---
    text = raw.decode(encoding or "utf-8", errors="replace")
    body: str
    if b"html" in content_type.encode() or b"xhtml" in content_type.encode():
        body = _html_to_markdown(text)
    elif b"json" in content_type.encode():
        # Return pretty-printed JSON — the agent can parse it.
        try:
            import json
            body = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.debug("JSON pretty-print failed, returning raw text: %s", exc)
            body = text
    else:
        # Plain text, markdown, SVG, etc.
        body = text

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
    if workspace_root is not None and len(body) > _INLINE_CHAR_LIMIT:
        return _offload(body, url, workspace_root)

    return body
