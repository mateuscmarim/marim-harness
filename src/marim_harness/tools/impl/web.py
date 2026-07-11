"""Web search tool backed by a self-hosted SearXNG instance."""

import os

import httpx

_DEFAULT_BASE_URL = "https://searxng.marim.dev"
_TIMEOUT = 15  # seconds
# A descriptive UA, matching fetch.py — some SearXNG deployments (and the bot
# filters in front of them) reject httpx's default User-Agent.
_UA = (
    "Mozilla/5.0 (compatible; marim-harness/1.0; "
    "+https://github.com/marim-dev/marim-harness)"
)
# Per-result snippet cap. Snippets are attacker-controlled (see the egress note
# below); SearXNG normally bounds them, but clamp defensively so one oversized
# `content` can't dominate the turn's context.
_SNIPPET_MAX = 300
# How much of an error response body to surface alongside the status line.
_ERROR_BODY_CHARS = 500


def _resolve_base_url(base_url: str | None) -> str:
    """Pick the SearXNG endpoint: an explicit *base_url*, else ``MARIM_SEARXNG_URL``
    from the environment, else the built-in default. The env override is
    operator-controlled (never model-supplied), so it stays on the trusted side of
    the SSRF boundary described in :func:`web_search`."""
    return base_url or os.environ.get("MARIM_SEARXNG_URL") or _DEFAULT_BASE_URL


async def web_search(  # noqa: C901  # complexity-debt: 2026-07-11 — see docs/superpowers/plans/2026-07-11-cyclomatic-complexity-reduction.md
    query: str,
    *,
    base_url: str | None = None,
    categories: str | None = None,
    max_results: int = 10,
) -> str:
    """Search the web via a SearXNG instance and return formatted results.

    *query* is the search string.  *categories* restricts results to a
    SearXNG category (e.g. ``"general"``, ``"images"``, ``"news"``,
    ``"science"``).  *max_results* caps how many hits are returned (default
    10, max 50).  *base_url* overrides the endpoint; when ``None`` it falls back
    to ``MARIM_SEARXNG_URL`` then the built-in default."""
    max_results = min(max(max_results, 1), 50)
    base_url = _resolve_base_url(base_url)

    # NOTE: egress here is intentionally NOT IP-pinned the way fetch.py is — this
    # talks to a single trusted, configured SearXNG instance, not an arbitrary
    # model-supplied URL. The results, however, ARE attacker-controlled (titles,
    # snippets, and especially `url`s the model may then pass to fetch_url). That
    # fetch_url hop is the prompt-injection boundary and is hardened there; keep
    # this in mind before making `base_url` model-controlled (it would become an
    # SSRF vector without fetch.py-style validation). The env/default resolution
    # above is operator-controlled, so it stays on the trusted side of that line.
    params: dict[str, str] = {"q": query, "format": "json"}
    if categories:
        params["categories"] = categories

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/search", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        msg = f"Search failed: HTTP {exc.response.status_code}"
        detail = (exc.response.text or "").strip()
        if detail:
            snippet = detail[:_ERROR_BODY_CHARS]
            if len(detail) > _ERROR_BODY_CHARS:
                snippet += "…"
            msg = f"{msg}\n{snippet}"
        return msg
    except httpx.RequestError as exc:
        return f"Search failed: {exc}"
    except ValueError:
        # resp.json() raises on a non-JSON body — SearXNG ships with the JSON
        # format disabled by default, so a misconfigured instance (or a rate-limit
        # / Cloudflare interstitial) returns HTML with HTTP 200. Surface that
        # rather than letting the decode error escape into the turn.
        return "Search failed: response was not valid JSON (is the SearXNG JSON format enabled?)"

    # A valid-JSON-but-wrong-shape response (bare array, null, …) would make the
    # `.get`/slice below throw; normalise both the envelope and the results list.
    results = data.get("results", []) if isinstance(data, dict) else []
    if not isinstance(results, list):
        results = []
    results = results[:max_results]
    if not results:
        return "No results found."

    lines: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "(no title)")
        url = r.get("url", "")
        snippet = r.get("content", "")
        if snippet and len(snippet) > _SNIPPET_MAX:
            snippet = snippet[:_SNIPPET_MAX] + "…"
        engines = ", ".join(r.get("engines", []))
        date = r.get("publishedDate") or ""
        header = f"{i}. {title}"
        if date:
            header += f"  ({date})"
        lines.append(header)
        lines.append(f"   {url}")
        if snippet:
            lines.append(f"   {snippet}")
        if engines:
            lines.append(f"   [engines: {engines}]")
        lines.append("")

    return "\n".join(lines)
