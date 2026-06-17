"""Web search tool backed by a self-hosted SearXNG instance."""

from typing import Optional

import httpx

_DEFAULT_BASE_URL = "https://searxng.marim.dev"
_TIMEOUT = 15  # seconds


async def web_search(
    query: str,
    *,
    base_url: str = _DEFAULT_BASE_URL,
    categories: Optional[str] = None,
    max_results: int = 10,
) -> str:
    """Search the web via a SearXNG instance and return formatted results.

    *query* is the search string.  *categories* restricts results to a
    SearXNG category (e.g. ``"general"``, ``"images"``, ``"news"``,
    ``"science"``).  *max_results* caps how many hits are returned (default
    10, max 50)."""
    max_results = min(max(max_results, 1), 50)

    params: dict = {"q": query, "format": "json"}
    if categories:
        params["categories"] = categories

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/search", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return f"Search failed: HTTP {exc.response.status_code}"
    except httpx.RequestError as exc:
        return f"Search failed: {exc}"

    results = data.get("results", [])[:max_results]
    if not results:
        return "No results found."

    lines: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "(no title)")
        url = r.get("url", "")
        snippet = r.get("content", "")
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
