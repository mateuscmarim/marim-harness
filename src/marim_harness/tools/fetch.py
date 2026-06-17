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

import hashlib
from pathlib import Path
from typing import Optional

import httpx
from markdownify import markdownify as md  # type: ignore[import-untyped]

_TIMEOUT = 30  # seconds
_MAX_BYTES = 1_000_000  # 1 MB — hard ceiling on what we download/write to disk
# When a workspace is available, a result larger than this is written to a file
# and the agent gets a handle + preview instead of the whole body inline — so a
# big page can't flood the turn's context. (read_file/grep can then page the
# file.) ~50k chars ≈ ~12k tokens; small results stay inline, no round-trip.
_INLINE_CHAR_LIMIT = 50_000
_PREVIEW_LINES = 40  # lines of the body shown in the handle for large pages
# Where offloaded fetch bodies live, relative to the workspace root. Gitignored.
_FETCH_DIR = (".marim", "fetch")
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
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    rel = Path(*_FETCH_DIR, f"{digest}.md")
    dest = workspace_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body)

    lines = body.splitlines()
    preview = "\n".join(lines[:_PREVIEW_LINES])
    rel_posix = rel.as_posix()
    return (
        f"# {_title_of(body, url)}\n"
        f"Fetched {url}\n\n"
        f"⚠️ Large page ({len(body):,} chars, {len(lines):,} lines) — full content "
        f"saved to `{rel_posix}`. Read more with read_file (it paginates) or grep "
        f"that path for what you need.\n\n"
        f"--- preview (first {min(_PREVIEW_LINES, len(lines))} lines) ---\n"
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

    if not body:
        return "Fetch succeeded but the page was empty."

    # --- offload large bodies so they don't flood context ---
    if workspace_root is not None and len(body) > _INLINE_CHAR_LIMIT:
        return _offload(body, url, workspace_root)

    return body
