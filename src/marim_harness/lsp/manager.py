"""Session-scoped pool of multilspy language servers plus the navigation and
diagnostics operations the LSP tools call.

One server per language, started lazily on first use and held open via an
``AsyncExitStack`` for the session. Every operation is timeout-bounded and never
raises into a tool: missing server, init failure, hung request, or empty result
all become short, human-readable strings the model can act on. Agent-facing
coordinates are 1-based; multilspy is 0-based, translated here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import checks, registry
from .diagnostics import DiagnosticsCollector, format_diagnostics

logger = logging.getLogger(__name__)

_MAX_RESULTS = 50

# The list-returning LSP tools bound their result *count* with _MAX_RESULTS, but
# hover returns a single blob whose width is unbounded — a long docstring or a
# deeply-generic type signature can be many KB. Clamp it so one hover can't flood
# context; the head carries the signature + first lines of docs, which is the part
# that matters.
_MAX_HOVER_CHARS = 4_000


def _default_factory(language: str, root: Path):
    """Build a real multilspy async LanguageServer for ``language`` at ``root``.
    Imported lazily so the heavy dependency loads only when a server is started."""
    from multilspy import LanguageServer
    from multilspy.multilspy_config import MultilspyConfig
    from multilspy.multilspy_logger import MultilspyLogger

    config = MultilspyConfig.from_dict({"code_language": language})
    return LanguageServer.create(config, MultilspyLogger(), str(root))


def _path_to_uri(root: Path, relpath: str) -> str:
    return (root / relpath).resolve().as_uri()


def _uri_to_rel(root: Path, uri: str) -> str:
    """Best-effort file URI → workspace-relative path; falls back to the absolute
    path if the target is outside the workspace."""
    if not uri:
        return "<unknown>"
    p = Path(unquote(urlparse(uri).path))
    try:
        return str(p.relative_to(root.resolve()))
    except ValueError:
        return str(p)


class LspManager:
    def __init__(
        self,
        root: Path,
        *,
        disabled: frozenset[str] = frozenset(),
        server_factory: Callable[[str, Path], Any] | None = None,
        request_timeout: float = 15.0,
        start_timeout: float = 60.0,
    ) -> None:
        self.root = root
        self._disabled = disabled
        self._factory = server_factory or _default_factory
        self._request_timeout = request_timeout
        self._start_timeout = start_timeout
        self._stack = AsyncExitStack()
        self._servers: dict[str, Any] = {}
        self._collectors: dict[str, DiagnosticsCollector] = {}
        # In-flight per-language startups, for the single-flight pattern in
        # _ensure_started. Deliberately NOT an asyncio.Lock: a lock would make a
        # slow startup for one language block callers for *every* language (and
        # the main agent's diagnostics-on-edit), which is exactly the fan-out
        # stall this map avoids.
        self._starts: dict[str, asyncio.Task[str | None]] = {}

    # --- lifecycle -----------------------------------------------------------

    async def _ensure_started(self, language: str) -> str | None:
        """Start the server for ``language`` exactly once, even under concurrent
        fan-out. The first caller for a language kicks off the startup task; every
        caller — the first included — awaits that same task, so duplicate servers
        are never spawned and a slow cold start is paid once, not once per caller.

        There is no shared lock: a startup for one language never blocks callers
        for a *different* language (or any other coroutine). This is what lets
        many sub-agents fan out across diagnostics without serializing. Returns
        None on success or a short error string. Callers must have already
        verified the language is eligible (not disabled, available)."""
        if language in self._servers:
            return None
        # Create-or-join the in-flight startup. There is no await between the
        # lookup and the store, so two coroutines can't both create a task for the
        # same language (asyncio is single-threaded) — that atomicity is the whole
        # reason a lock isn't needed here.
        task = self._starts.get(language)
        if task is None:
            task = asyncio.ensure_future(self._start_language(language))
            self._starts[language] = task
        try:
            return await task
        finally:
            # Drop the slot once the startup has *resolved*: on success
            # ``_servers`` short-circuits future calls; on failure a later call is
            # free to retry. Gate on ``task.done()`` so an awaiter cancelled while
            # the start is still in flight doesn't evict a live task (which would
            # let a fresh caller spawn a second server).
            if task.done() and self._starts.get(language) is task:
                self._starts.pop(language, None)

    async def _start_language(self, language: str) -> str | None:
        """Start and register the server for ``language``. Returns None on success
        or a short error string on failure. Always invoked through
        ``_ensure_started``, which guarantees exactly one concurrent call per
        language, so this needs no lock of its own."""
        try:
            server = self._factory(language, self.root)
            await asyncio.wait_for(
                self._stack.enter_async_context(server.start_server()),
                timeout=self._start_timeout,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to a message
            logger.debug("failed to start %s server: %s", language, exc)
            return f"Could not start the {language} language server: {exc}"
        collector = DiagnosticsCollector()
        collector.attach(server)
        self._servers[language] = server
        self._collectors[language] = collector
        return None

    async def _server_for(
        self, path: str
    ) -> tuple[Any | None, str | None, str | None]:
        """Return (server, language, error_message). On any problem the server is
        None and error_message is a string to hand back to the model."""
        language = registry.language_for(path)
        if language is None:
            return None, None, f"No language server for {path!r} (unsupported file type)."
        if language in self._disabled:
            return None, language, f"LSP is disabled for {language}."
        avail = registry.availability(language)
        if not avail.available:
            return None, language, f"No {language} language server available; {avail.hint}."
        if language in self._servers:
            return self._servers[language], language, None
        err = await self._ensure_started(language)
        if err:
            return None, language, err
        return self._servers[language], language, None

    async def aclose(self) -> None:
        """Shut down every started language server. Safe to call when none ran."""
        try:
            await self._stack.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.debug("error during LSP shutdown: %s", exc)
        self._servers.clear()
        self._collectors.clear()

    # --- helpers -------------------------------------------------------------

    async def _call(self, coro, what: str) -> tuple[Any | None, str | None]:
        """Await ``coro`` under the request timeout. Returns (result, error)."""
        try:
            return await asyncio.wait_for(coro, timeout=self._request_timeout), None
        except asyncio.TimeoutError:
            return None, f"{what}: language server timed out."
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s failed: %s", what, exc)
            return None, f"{what}: {exc}"

    def _format_locations(self, label: str, locs) -> str:
        items = locs or []
        if not items:
            return f"No {label} found."
        out: list[str] = []
        for loc in items[:_MAX_RESULTS]:
            uri = str(loc.get("uri") or loc.get("absolutePath") or "")
            start = loc.get("range", {}).get("start", {})
            rel = (
                _uri_to_rel(self.root, uri) if uri.startswith("file:") else (uri or "<unknown>")
            )
            out.append(f"{rel}:{start.get('line', 0) + 1}:{start.get('character', 0) + 1}")
        extra = len(items) - _MAX_RESULTS
        if extra > 0:
            out.append(f"… and {extra} more")
        return "\n".join(out)

    # --- operations ----------------------------------------------------------

    async def _require_server(self, path: str, what: str) -> tuple[Any, str | None]:
        """Return ``(server, None)`` if a server is available for ``path``, or
        ``(None, error_str)`` with a caller-ready message otherwise."""
        server, _lang, err = await self._server_for(path)
        if err:
            return None, err
        if server is None:
            return None, f"{what}: no server available for {path!r}."
        return server, None

    async def goto_definition(self, path: str, line: int, col: int) -> str:
        server, err = await self._require_server(path, "goto_definition")
        if err:
            return err
        res, err = await self._call(
            server.request_definition(path, line - 1, col - 1), "goto_definition"
        )
        return err or self._format_locations("definitions", res)

    async def find_references(self, path: str, line: int, col: int) -> str:
        server, err = await self._require_server(path, "find_references")
        if err:
            return err
        res, err = await self._call(
            server.request_references(path, line - 1, col - 1), "find_references"
        )
        return err or self._format_locations("references", res)

    async def hover(self, path: str, line: int, col: int) -> str:
        server, err = await self._require_server(path, "hover")
        if err:
            return err
        res, err = await self._call(
            server.request_hover(path, line - 1, col - 1), "hover"
        )
        if err:
            return err
        return _clamp_hover(_hover_text(res)) or "No hover information."

    async def document_symbols(self, path: str) -> str:
        server, err = await self._require_server(path, "document_symbols")
        if err:
            return err
        res, err = await self._call(
            server.request_document_symbols(path), "document_symbols"
        )
        if err:
            return err
        return _format_symbols(res) or "No symbols found."

    async def workspace_symbols(self, query: str) -> str:
        """Search every language server already started this session; if none are
        running, start the locally-installed servers (cheap — no auto-downloads)
        and search those. Aggregates results across languages."""
        if not self._servers:
            for language in sorted(registry.locally_installed_languages()):
                if language in self._disabled:
                    continue
                await self._start_named(language)
        if not self._servers:
            return "No language server is active; open a file with another LSP tool first."
        lines: list[str] = []
        for language, server in list(self._servers.items()):
            res, err = await self._call(
                server.request_workspace_symbol(query), f"workspace_symbols[{language}]"
            )
            if err or not res:
                continue
            for sym in res[:_MAX_RESULTS]:
                loc = sym.get("location", {})
                uri = loc.get("uri", "")
                start = loc.get("range", {}).get("start", {})
                rel = _uri_to_rel(self.root, uri)
                lines.append(f"{sym.get('name', '?')}  {rel}:{start.get('line', 0) + 1}")
        return "\n".join(lines[:_MAX_RESULTS]) if lines else f"No symbols matching {query!r}."

    async def _start_named(self, language: str) -> None:
        """Lazily start a server by language name (no path), for workspace_symbols.
        Delegates to _ensure_started (single-flight); failures are swallowed
        (best-effort)."""
        await self._ensure_started(language)  # ignore returned error — best-effort

    async def diagnostics(self, path: str, *, settle: float = 1.5, deep: bool = False) -> str:
        # Python's resident server (jedi) only reports syntax errors, so route it
        # to real external checkers instead — ruff always, plus pyright on a deep
        # check (see lsp.checks). These are stateless subprocesses: no server
        # startup, no shared state, so they never block other diagnostics callers
        # and fan out across sub-agents for free. ``settle`` is unused here (it
        # paces the LSP push path below, not a subprocess).
        if registry.language_for(path) == "python":
            diags = await checks.python_diagnostics(self.root, path, deep=deep)
            return checks.format_checks(path, diags)
        server, language, err = await self._server_for(path)
        if err:
            return err
        if server is None:
            return f"diagnostics: no server available for {path!r}."
        collector = self._collectors.get(language or "")
        if collector is None or not collector.enabled:
            return f"{path}: diagnostics unavailable for this language server."
        uri = _path_to_uri(self.root, path)

        async def _open_and_wait():
            with server.open_file(path):  # didOpen → server pushes diagnostics
                await asyncio.sleep(settle)

        _res, err = await self._call(_open_and_wait(), "diagnostics")
        if err:
            return err
        return format_diagnostics(path, collector.latest(uri))


def _clamp_hover(text: str) -> str:
    """Cap hover text to ``_MAX_HOVER_CHARS``, keeping the head (signature + start
    of the docs) and noting how much was dropped."""
    if len(text) <= _MAX_HOVER_CHARS:
        return text
    dropped = len(text) - _MAX_HOVER_CHARS
    return f"{text[:_MAX_HOVER_CHARS]}\n… ({dropped} more chars truncated)"


def _hover_text(res) -> str:
    """Extract readable text from an LSP hover result (contents may be a string,
    a {value}/{language,value} dict, or a list of those)."""
    if not res:
        return ""
    contents = res.get("contents", res) if isinstance(res, dict) else res
    return _stringify_markup(contents)


def _stringify_markup(contents) -> str:
    if isinstance(contents, str):
        return contents.strip()
    if isinstance(contents, dict):
        return str(contents.get("value", "")).strip()
    if isinstance(contents, list):
        return "\n".join(p for p in (_stringify_markup(c) for c in contents) if p)
    return ""


# LSP SymbolKind -> short label (only the common ones; others fall back to the number).
_SYMBOL_KIND = {
    5: "class", 6: "method", 8: "field", 9: "constructor",
    11: "interface", 12: "function", 13: "variable", 14: "constant",
    23: "struct", 26: "type",
}


def _format_symbols(res) -> str:
    """Format request_document_symbols output. multilspy may return a list of
    symbols or a (symbols, roots) tuple; handle both."""
    symbols = res[0] if isinstance(res, tuple) else res
    if not symbols:
        return ""
    out: list[str] = []
    for sym in list(symbols)[:_MAX_RESULTS]:
        kind = _SYMBOL_KIND.get(sym.get("kind"), str(sym.get("kind", "")))
        rng = sym.get("range") or sym.get("location", {}).get("range", {})
        line = rng.get("start", {}).get("line", 0) + 1
        out.append(f"{kind} {sym.get('name', '?')}  :{line}")
    return "\n".join(out)
