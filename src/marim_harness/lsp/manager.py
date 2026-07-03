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
import contextlib
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

# How long to wait for further diagnostics after a publish before concluding the
# server has finished. Many language servers publish in phases — tsserver/gopls/
# rust-analyzer emit an empty set on didOpen, then the real diagnostics once they
# finish parsing/checking — so returning on the first publish would report a false
# "no diagnostics". After the first publish we wait out this quiet gap for more
# (bounded by the overall ``settle`` ceiling) and read whatever landed last.
_PUBLISH_QUIESCE = 0.2


def _default_factory(language: str, root: Path):
    """Build a real multilspy async LanguageServer for ``language`` at ``root``.
    Imported lazily so the heavy dependency loads only when a server is started."""
    from multilspy import LanguageServer
    from multilspy.multilspy_config import MultilspyConfig
    from multilspy.multilspy_logger import MultilspyLogger

    config = MultilspyConfig.from_dict({"code_language": language})
    return LanguageServer.create(config, MultilspyLogger(), str(root))


def _server_alive(server: Any) -> bool:
    """Whether a warm multilspy server's process is still up. multilspy sets
    ``server_started`` True while the LSP subprocess runs and flips it False when
    the start_server context exits / the process is gone. When the attribute is
    absent (a stub or older multilspy) we assume alive: only a *known*-dead server
    is ever evicted, never one we merely can't inspect."""
    return bool(getattr(server, "server_started", True))


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
        self._closed = False
        self._servers: dict[str, Any] = {}
        self._collectors: dict[str, DiagnosticsCollector] = {}
        # Per-URI publish waiters for the diagnostics push path. A diagnostics()
        # call registers an asyncio.Event under the document URI before it opens the
        # file; the notification wrapper installed in _start_language sets every
        # event waiting on that URI the moment the server pushes diagnostics for it,
        # so the call wakes on the publish instead of sleeping the full settle
        # window. Keyed by URI → the events currently waiting on it.
        self._publish_waiters: dict[str, list[asyncio.Event]] = {}
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
        if self._closed:
            # Lost the race with aclose(): our server entered ``_stack`` but the
            # session is tearing down. Registering now would resurrect it into the
            # just-cleared pool and leak the process past aclose() (a cold start can
            # take up to start_timeout, well past a Ctrl-C shutdown). The stack's
            # own aclose() reaps what we entered, so decline instead of caching it.
            return f"Could not start the {language} language server: manager is closing."
        collector = DiagnosticsCollector()
        collector.attach(server)
        self._servers[language] = server
        self._collectors[language] = collector
        self._install_publish_signal(server, collector)
        return None

    def _install_publish_signal(self, server: Any, collector: DiagnosticsCollector) -> None:
        """Layer a wakeup signal over the diagnostics collector's notification
        handler. ``DiagnosticsCollector`` registers a *single* publishDiagnostics
        handler on the multilspy server (the underlying ``on_notification`` is a
        one-slot dict), so to also wake a waiting ``diagnostics()`` call we
        re-register a wrapper that:

        1. feeds the collector exactly as before — MANDATORY: skip this and
           ``collector.latest()`` goes permanently stale, so every push-path
           diagnostics call would report 'no diagnostics'; and
        2. releases any event waiting on the published URI, so the open-and-wait in
           ``diagnostics()`` returns the instant diagnostics land.

        Best-effort and guarded the same way the collector is: when the notification
        surface isn't present (older multilspy, or ``attach`` failed), the collector's
        own handler is left untouched and ``diagnostics()`` simply waits out the
        settle ceiling — never worse than the old fixed sleep."""
        if not collector.enabled:
            return
        inner = getattr(server, "server", None)
        on_notification = getattr(inner, "on_notification", None)
        if on_notification is None:
            return

        async def _handler(params, *_rest) -> None:
            # Keep the collector's per-URI cache current first; diagnostics() reads
            # it via collector.latest() once woken.
            collector._on_publish(params)
            uri = params.get("uri") if isinstance(params, dict) else None
            if uri is not None:
                for event in self._publish_waiters.get(uri, ()):
                    event.set()

        try:
            on_notification("textDocument/publishDiagnostics", _handler)
        except Exception as exc:  # noqa: BLE001 — degrade to the settle ceiling
            logger.debug("failed to install diagnostics wakeup: %s", exc)

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
        # A warm/running server short-circuits *before* the availability probe:
        # registry.availability() runs shutil.which over the language's probe
        # binaries (a PATH scan), and there's no point paying that on the hot path
        # when the server is already up. Availability only gates the cold start.
        if language in self._servers:
            server = self._servers[language]
            if _server_alive(server):
                return server, language, None
            # The server was up but its process is gone (multilspy flipped
            # ``server_started`` False). Left cached, every later request for this
            # language would route to a corpse and return the same error for the
            # rest of the session, with no restart path. Evict it so the cold-start
            # path below re-spawns a fresh one — the single-flight in
            # _ensure_started still coalesces concurrent callers onto one restart.
            self._evict(language)
        avail = registry.availability(language)
        if not avail.available:
            return None, language, f"No {language} language server available; {avail.hint}."
        err = await self._ensure_started(language)
        if err:
            return None, language, err
        return self._servers[language], language, None

    def _evict(self, language: str) -> None:
        """Drop a language's cached server/collector so the next request cold-starts
        a fresh one. Used when a warm server is found dead."""
        self._servers.pop(language, None)
        self._collectors.pop(language, None)

    async def aclose(self) -> None:
        """Shut down every started language server. Safe to call when none ran."""
        # Latch closed *first* so a cold start still in flight (a start can take up
        # to start_timeout — 60s — well past a Ctrl-C shutdown) refuses to register
        # its server after the pool is torn down. Without this a start racing
        # shutdown finishes after _stack.aclose(), re-populates the cleared
        # _servers, and its language-server process leaks until harness exit.
        self._closed = True
        # Cancel and reap in-flight single-flight starts. A start suspended in
        # enter_async_context is unwound by the cancel (its half-entered server
        # never lands on the stack); one already past that await is caught by the
        # _closed guard in _start_language. Either way nothing new enters _servers
        # after this. Reap before _stack.aclose() so no start can still be touching
        # the stack while we close it. (_starts is otherwise never cleared, so a
        # dead task would linger for the manager's lifetime.)
        for task in list(self._starts.values()):
            task.cancel()
        for task in list(self._starts.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._starts.clear()
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
            # The external-checker shortcut must still honor the per-language
            # disable switch — "LSP off for python" means no diagnostics
            # subprocesses either, matching every other operation's gate.
            if "python" in self._disabled:
                return "LSP is disabled for python."
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

        # Register the wakeup BEFORE opening the file: didOpen makes the server push
        # diagnostics, and we want to catch that publish even if it lands the instant
        # the open completes (single-threaded loop, so a set() before our wait() is
        # not missed). ``settle`` is now only a *ceiling* — we return shortly after
        # the server stops publishing for this URI, instead of always sleeping the
        # full window.
        event = asyncio.Event()
        self._publish_waiters.setdefault(uri, []).append(event)

        async def _open_and_wait():
            # didOpen makes the server push diagnostics; wake on that publish, then
            # wait out a short quiet gap (_PUBLISH_QUIESCE) for follow-up publishes
            # before returning — servers that publish empty-then-real would
            # otherwise have their empty first push read as "no diagnostics". The
            # whole wait is bounded by ``settle``, and wait_for always returns or
            # raises TimeoutError, so this can never hang past that ceiling. We read
            # collector.latest() after returning, so it always reflects the most
            # recent publish regardless of how many arrived.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + settle
            with server.open_file(path):
                got_publish = False
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    # Before the first publish, wait the whole remaining window; once
                    # one has landed, only wait out the short quiesce gap for more.
                    timeout = min(_PUBLISH_QUIESCE, remaining) if got_publish else remaining
                    event.clear()
                    try:
                        await asyncio.wait_for(event.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        # settle elapsed with nothing pushed, or publishes quiesced
                        # after at least one — either way we're done.
                        break
                    got_publish = True

        try:
            _res, err = await self._call(_open_and_wait(), "diagnostics")
        finally:
            waiters = self._publish_waiters.get(uri)
            if waiters is not None:
                with contextlib.suppress(ValueError):
                    waiters.remove(event)
                if not waiters:
                    self._publish_waiters.pop(uri, None)
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
