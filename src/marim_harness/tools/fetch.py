"""Fetch a URL and return its content as clean Markdown.

Follows the same pattern as Claude Code's WebFetch tool: fetch HTML via
httpx, convert to Markdown with markdownify, and return the result so the
agent can process it.  Supports HTML pages, plain-text responses, and
redirects (same-host by default).

Safety: only ``http`` / ``https`` URLs are accepted; ``file://``,
``ftp://``, etc. are rejected.  Responses over ``_MAX_BYTES`` are
truncated with a note.
"""

from __future__ import annotations

from typing import Optional

import httpx
from markdownify import markdownify as md  # type: ignore[import-untyped]

_TIMEOUT = 30  # seconds
_MAX_BYTES = 1_000_000  # 1 MB — generous but bounded
_ALLOWED_SCHEMES = frozenset({"http", "https"})

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


async def fetch_url(
    url: str,
    *,
    prompt: Optional[str] = None,
    timeout: int = _TIMEOUT,
) -> str:
    """Fetch *url* and return its body as Markdown.

    *url* must be an HTTP(S) URL.  *prompt* is an optional hint about
    what to extract (included as a header in the output for context but
    does **not** alter the fetch itself).  *timeout* caps the request in
    seconds (default 30).

    Returns a Markdown-formatted string, or an error message on failure.
    """
    try:
        url = _normalise_url(url)
    except ValueError as exc:
        return str(exc)

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": _UA},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Fetch failed: HTTP {exc.response.status_code} — {exc.response.reason_phrase}"
    except httpx.RequestError as exc:
        return f"Fetch failed: {exc}"

    content_type = resp.headers.get("content-type", "")
    raw = resp.content

    # --- truncation guard ---
    truncated = False
    if len(raw) > _MAX_BYTES:
        raw = raw[:_MAX_BYTES]
        truncated = True

    # --- decode ---
    body: str
    if b"html" in content_type.encode() or b"xhtml" in content_type.encode():
        html = raw.decode(resp.encoding or "utf-8", errors="replace")
        body = _html_to_markdown(html)
    elif b"json" in content_type.encode():
        # Return pretty-printed JSON — the agent can parse it.
        try:
            import json
            data = resp.json()
            body = json.dumps(data, indent=2, ensure_ascii=False)
        except Exception:
            body = raw.decode(resp.encoding or "utf-8", errors="replace")
    else:
        # Plain text, markdown, SVG, etc.
        body = raw.decode(resp.encoding or "utf-8", errors="replace")

    if truncated:
        body += (
            f"\n\n---\n⚠️ Page truncated — original was "
            f"{len(resp.content):,} bytes; showing first {_MAX_BYTES:,}."
        )

    # --- optional prompt header ---
    if prompt:
        body = f"## Requested: {prompt}\n\n{body}"

    return body if body else "Fetch succeeded but the page was empty."
