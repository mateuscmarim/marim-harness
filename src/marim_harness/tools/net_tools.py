from pydantic_ai import RunContext

from ..runtime.deps import Deps
from . import fetch, web


async def fetch_url(
    ctx: RunContext[Deps],
    url: str,
    prompt: str | None = None,
) -> str:
    """Fetch and read content from a specific URL to augment context with live web
    content. Returns the page body as clean Markdown. Accepts a URL (http/https)
    and an optional `prompt` describing what to extract or look for.

    Use this when you need the actual content of a page — web_search only returns
    titles and snippets. HTML pages are converted to Markdown; JSON is
    pretty-printed; plain text is returned as-is. A large page is saved to a file
    under the workspace and you get a handle + preview back — read_file/grep that
    path to page through it — so it doesn't flood context."""
    return await fetch.fetch_url(
        url, prompt=prompt, workspace_root=ctx.deps.workspace.root
    )


async def web_search(
    ctx: RunContext[Deps],
    query: str,
    categories: str | None = None,
    max_results: int = 10,
) -> str:
    """Search the web via a self-hosted SearXNG instance and return formatted results.

    *query* is the search string.  *categories* restricts results to a SearXNG
    category (e.g. "general", "images", "news", "science").  *max_results*
    caps how many hits are returned (default 10, max 50)."""
    return await web.web_search(query, categories=categories, max_results=max_results)
