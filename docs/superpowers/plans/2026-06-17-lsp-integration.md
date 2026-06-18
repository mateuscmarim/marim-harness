# LSP Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the harness semantic code intelligence (go-to-definition, find-references, hover, document/workspace symbols) plus best-effort diagnostics-on-edit, backed by language servers via `multilspy`, covering Python / TypeScript-JavaScript / Java / C++.

**Architecture:** A new session-scoped subpackage `src/marim_harness/lsp/` wraps `multilspy`: a `registry` maps file extensions to language ids and reports server availability; an `LspManager` lazily starts one server per language, holds them open for the session, translates coordinates (1-based ↔ 0-based), formats results, and bounds every call with a timeout so failures degrade to short strings instead of raising. Six read-only tools in `tools/provider.py` call the manager through `ctx.deps.lsp`; `write_file`/`edit_file` append best-effort diagnostics. The Harness constructs the manager, wires it onto `Deps`, and tears it down in `aclose()`.

**Tech Stack:** Python ≥3.10, `multilspy`, pydantic-ai, pytest + anyio (asyncio backend), ruff, pyright.

## Global Constraints

- `requires-python = ">=3.10"` — do NOT use `asyncio.timeout` (3.11+); use `asyncio.wait_for`.
- Ruff: `line-length = 100`, lint rules `E, F, I` (import sorting enforced).
- Pyright: `typeCheckingMode = "basic"`, `pythonVersion = "3.10"`, includes `src` only.
- Tests use `@pytest.mark.anyio` for async (backend fixture is in `tests/conftest.py`, returns `"asyncio"`).
- Test command: `uv run pytest <path> -v`. Lint: `uv run ruff check`. Types: `uv run pyright`.
- All LSP operations are **best-effort**: missing server / init failure / hung request / empty result return a short human-readable string, never raise into a tool.
- Agent-facing coordinates are **1-based** (matching `read_file`/`grep` output); `multilspy` is **0-based**.
- Do NOT import `multilspy` at module top-level in `registry.py` or `tools/provider.py` — only inside `LspManager` (so importing tools never drags in the heavy dep or spawns servers). Tests inject a fake server factory and never import `multilspy`.

---

### Task 1: Language registry

**Files:**
- Create: `src/marim_harness/lsp/__init__.py`
- Create: `src/marim_harness/lsp/registry.py`
- Test: `tests/test_lsp_registry.py`

**Interfaces:**
- Consumes: nothing (leaf module, stdlib only).
- Produces:
  - `language_for(path: str) -> str | None` — multilspy `code_language` for a file path, else None.
  - `@dataclass(frozen=True) Availability(available: bool, hint: str)`
  - `availability(language: str) -> Availability` — can a server for this language start, with an install hint.
  - `locally_installed_languages() -> set[str]` — languages whose probe binary is on PATH (excludes auto-download-only langs like java).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lsp_registry.py
from marim_harness.lsp import registry


def test_language_for_known_extensions():
    assert registry.language_for("src/mod.py") == "python"
    assert registry.language_for("a/b/Comp.tsx") == "typescript"
    assert registry.language_for("x.ts") == "typescript"
    assert registry.language_for("x.jsx") == "javascript"
    assert registry.language_for("Main.java") == "java"
    assert registry.language_for("engine.cpp") == "cpp"
    assert registry.language_for("util.hpp") == "cpp"


def test_language_for_unknown_or_extensionless():
    assert registry.language_for("README.md") is None
    assert registry.language_for("Makefile") is None
    assert registry.language_for("noext") is None


def test_language_for_is_case_insensitive():
    assert registry.language_for("FOO.PY") == "python"


def test_availability_unsupported_language():
    a = registry.availability("cobol")
    assert a.available is False
    assert a.hint


def test_availability_auto_provided_language_is_available():
    # java is auto-downloaded by multilspy; no PATH probe required.
    assert registry.availability("java").available is True


def test_availability_path_probed(monkeypatch):
    monkeypatch.setattr(registry.shutil, "which", lambda b: "/usr/bin/clangd" if b == "clangd" else None)
    assert registry.availability("cpp").available is True
    monkeypatch.setattr(registry.shutil, "which", lambda b: None)
    cpp = registry.availability("cpp")
    assert cpp.available is False
    assert "clangd" in cpp.hint


def test_locally_installed_excludes_auto_download(monkeypatch):
    monkeypatch.setattr(registry.shutil, "which", lambda b: "/x" if b == "clangd" else None)
    langs = registry.locally_installed_languages()
    assert "cpp" in langs
    assert "java" not in langs  # java has no PATH probe (auto-download only)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lsp_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.lsp'`

- [ ] **Step 3: Create the package marker**

```python
# src/marim_harness/lsp/__init__.py
"""LSP integration: language-server-backed navigation and diagnostics."""
```

- [ ] **Step 4: Implement the registry**

```python
# src/marim_harness/lsp/registry.py
"""Map workspace files to multilspy language ids and report server availability.

Pure stdlib + small helpers, with no ``multilspy`` import, so importing the
registry (e.g. from the tools module) never drags in the heavy dependency or
spawns a language server.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

# File extension (lowercase, including dot) -> multilspy ``code_language``.
_EXT_TO_LANG = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".java": "java",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
}

# language -> (PATH probe binaries, install hint). A language with a non-empty
# probe tuple is "available" only when one of its binaries is on PATH. A language
# with an empty probe tuple is auto-provided by multilspy (it downloads the
# server on first use) and is always reported available.
_PROBES: dict[str, tuple[tuple[str, ...], str]] = {
    "python": (("pyright-langserver", "pyright"), "install pyright (npm i -g pyright)"),
    "typescript": (
        ("typescript-language-server",),
        "install typescript-language-server (npm i -g typescript-language-server typescript)",
    ),
    "javascript": (
        ("typescript-language-server",),
        "install typescript-language-server (npm i -g typescript-language-server typescript)",
    ),
    "cpp": (("clangd",), "install clangd (e.g. pacman -S clang)"),
    "java": ((), "auto-downloaded by multilspy on first use"),
}


def language_for(path: str) -> str | None:
    """Return the multilspy ``code_language`` for ``path``, or None if the file
    extension isn't one we support."""
    dot = path.rfind(".")
    if dot == -1:
        return None
    return _EXT_TO_LANG.get(path[dot:].lower())


@dataclass(frozen=True)
class Availability:
    available: bool
    hint: str


def availability(language: str) -> Availability:
    """Whether a server for ``language`` can be started, with an install hint."""
    entry = _PROBES.get(language)
    if entry is None:
        return Availability(False, "unsupported language")
    probes, hint = entry
    if not probes:  # auto-provided by multilspy
        return Availability(True, hint)
    found = any(shutil.which(b) for b in probes)
    return Availability(found, hint)


def locally_installed_languages() -> set[str]:
    """Languages whose server binary is on PATH right now. Excludes
    auto-download-only languages (e.g. java) so callers can cheaply start
    every locally-present server without triggering a multi-hundred-MB download."""
    out: set[str] = set()
    for language, (probes, _hint) in _PROBES.items():
        if probes and any(shutil.which(b) for b in probes):
            out.add(language)
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_lsp_registry.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check src/marim_harness/lsp tests/test_lsp_registry.py && uv run pyright src/marim_harness/lsp/registry.py`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/lsp/__init__.py src/marim_harness/lsp/registry.py tests/test_lsp_registry.py
git commit -m "feat(lsp): language registry — extension→language map and server availability"
```

---

### Task 2: Diagnostics collector and formatter

**Files:**
- Create: `src/marim_harness/lsp/diagnostics.py`
- Test: `tests/test_lsp_diagnostics.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces:
  - `class DiagnosticsCollector` with `enabled: bool`, `attach(server) -> None`, `latest(uri: str) -> list[dict]`.
  - `format_diagnostics(path: str, diags: list[dict], *, max_results: int = 50) -> str`.

The collector registers a `textDocument/publishDiagnostics` notification handler on multilspy's underlying server object (an internal API), guarded so a version mismatch degrades to "disabled" rather than crashing. Its `_on_publish` accepts variadic args and picks the first dict containing a `"uri"` key, tolerating both `handler(params)` and `handler(server, params)` call shapes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lsp_diagnostics.py
from marim_harness.lsp.diagnostics import DiagnosticsCollector, format_diagnostics


class _FakeServer:
    """Stands in for multilspy's LanguageServer: exposes a nested ``server``
    handler with ``on_notification(method, handler)``."""

    def __init__(self):
        self._handlers = {}

        class _Handler:
            def on_notification(_self, method, handler):
                self._handlers[method] = handler

        self.server = _Handler()

    def publish(self, uri, diags):
        self._handlers["textDocument/publishDiagnostics"]({"uri": uri, "diagnostics": diags})


def test_collector_attaches_and_collects():
    srv = _FakeServer()
    c = DiagnosticsCollector()
    c.attach(srv)
    assert c.enabled is True
    srv.publish("file:///x/a.py", [{"severity": 1, "message": "boom"}])
    assert c.latest("file:///x/a.py") == [{"severity": 1, "message": "boom"}]
    assert c.latest("file:///x/missing.py") == []


def test_collector_disabled_when_no_handler_api():
    class Bare:
        server = object()  # no on_notification

    c = DiagnosticsCollector()
    c.attach(Bare())
    assert c.enabled is False


def test_collector_handler_tolerates_extra_leading_arg():
    srv = _FakeServer()
    c = DiagnosticsCollector()
    c.attach(srv)
    # Simulate a handler(server, params) call shape.
    c._on_publish(object(), {"uri": "file:///y.py", "diagnostics": [{"severity": 2, "message": "warn"}]})
    assert c.latest("file:///y.py") == [{"severity": 2, "message": "warn"}]


def test_format_no_diagnostics():
    assert format_diagnostics("a.py", []) == "a.py: no diagnostics"


def test_format_lists_severity_and_position():
    diags = [
        {"severity": 1, "message": "undefined name", "range": {"start": {"line": 4, "character": 2}}},
        {"severity": 2, "message": "unused import\nsecond line", "range": {"start": {"line": 0, "character": 0}}},
    ]
    out = format_diagnostics("a.py", diags)
    assert "a.py:5:3: error: undefined name" in out
    assert "a.py:1:1: warning: unused import" in out
    assert "second line" not in out  # only first message line kept


def test_format_truncates():
    diags = [{"severity": 1, "message": f"m{i}", "range": {"start": {"line": i, "character": 0}}} for i in range(60)]
    out = format_diagnostics("a.py", diags, max_results=10)
    assert "… and 50 more" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lsp_diagnostics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.lsp.diagnostics'`

- [ ] **Step 3: Implement the collector and formatter**

```python
# src/marim_harness/lsp/diagnostics.py
"""Capture and format LSP diagnostics.

LSP diagnostics are *pushed* by the server (``textDocument/publishDiagnostics``)
after a file opens or changes — they are not request/response. The collector
registers a notification handler on multilspy's underlying server object (an
internal API) and stashes the latest diagnostics per file URI. Registration is
guarded so a multilspy version without that surface degrades to disabled rather
than raising.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# LSP DiagnosticSeverity -> label.
_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


class DiagnosticsCollector:
    """Per-server sink for pushed diagnostics, keyed by file URI."""

    def __init__(self) -> None:
        self._by_uri: dict[str, list[dict]] = {}
        self.enabled = False

    def attach(self, server) -> None:
        """Register the publishDiagnostics handler on ``server``'s underlying
        notification API. Best-effort: leaves ``enabled`` False if the API isn't
        present or registration fails."""
        handler = getattr(server, "server", None)
        on_notification = getattr(handler, "on_notification", None)
        if on_notification is None:
            logger.debug("multilspy server has no on_notification; diagnostics disabled")
            return
        try:
            on_notification("textDocument/publishDiagnostics", self._on_publish)
            self.enabled = True
        except Exception as exc:  # noqa: BLE001 — degrade, never crash a session
            logger.debug("failed to register diagnostics handler: %s", exc)

    def _on_publish(self, *args) -> None:
        params = next((a for a in args if isinstance(a, dict) and "uri" in a), None)
        if params is None:
            return
        self._by_uri[params["uri"]] = params.get("diagnostics") or []

    def latest(self, uri: str) -> list[dict]:
        return self._by_uri.get(uri, [])


def format_diagnostics(path: str, diags: list[dict], *, max_results: int = 50) -> str:
    """Render diagnostics as ``path:line:col: severity: message`` lines (1-based,
    first message line only). Empty list → a clear 'no diagnostics' note."""
    if not diags:
        return f"{path}: no diagnostics"
    lines: list[str] = []
    for d in diags[:max_results]:
        sev = _SEVERITY.get(d.get("severity", 1), "error")
        start = d.get("range", {}).get("start", {})
        line = start.get("line", 0) + 1
        col = start.get("character", 0) + 1
        msg = (d.get("message") or "").splitlines()
        head = msg[0] if msg else ""
        lines.append(f"{path}:{line}:{col}: {sev}: {head}")
    extra = len(diags) - max_results
    if extra > 0:
        lines.append(f"… and {extra} more")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_lsp_diagnostics.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/marim_harness/lsp/diagnostics.py tests/test_lsp_diagnostics.py && uv run pyright src/marim_harness/lsp/diagnostics.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/lsp/diagnostics.py tests/test_lsp_diagnostics.py
git commit -m "feat(lsp): diagnostics collector + compact formatter"
```

---

### Task 3: LspManager — lifecycle, routing, navigation, formatting

**Files:**
- Modify: `pyproject.toml` (add `multilspy` dependency)
- Create: `src/marim_harness/lsp/manager.py`
- Test: `tests/test_lsp_manager.py`

**Interfaces:**
- Consumes: `registry.language_for`, `registry.availability`, `registry.locally_installed_languages`; `diagnostics.DiagnosticsCollector`, `diagnostics.format_diagnostics`.
- Produces — `class LspManager`:
  - `__init__(self, root: Path, *, disabled: frozenset[str] = frozenset(), server_factory: Callable[[str, Path], object] | None = None, request_timeout: float = 15.0, start_timeout: float = 60.0)`
  - `async goto_definition(path, line, col) -> str`
  - `async find_references(path, line, col) -> str`
  - `async hover(path, line, col) -> str`
  - `async document_symbols(path) -> str`
  - `async workspace_symbols(query: str) -> str`
  - `async diagnostics(path, *, settle: float = 1.5) -> str`
  - `async aclose() -> None`

`server_factory(language, root)` returns an object exposing async `request_definition/request_references/request_hover/request_document_symbols/request_workspace_symbol(relpath, line, col)`, a sync `open_file(relpath)` context manager, an `async start_server()` context manager, and a nested `.server.on_notification`. The default factory builds a real `multilspy` `LanguageServer`; tests inject a fake. Coordinates passed to the factory's request methods are **0-based**.

- [ ] **Step 1: Add the dependency**

Edit `pyproject.toml` `[project].dependencies` to add `multilspy` after `"textual>=0.80",`:

```toml
    "textual>=0.80",
    "multilspy>=0.0.10",
```

Then run: `uv sync`
Expected: `multilspy` resolves and installs.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_lsp_manager.py
import contextlib
from pathlib import Path

import pytest

from marim_harness.lsp.manager import LspManager


class _FakeServer:
    """Implements the multilspy surface LspManager relies on. Records the
    (line, col) it was asked for so the test can assert 0-based translation."""

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[tuple] = []
        self.started = 0

        class _Handler:
            def on_notification(self, method, handler):
                pass

        self.server = _Handler()

    @contextlib.asynccontextmanager
    async def start_server(self):
        self.started += 1
        yield self

    @contextlib.contextmanager
    def open_file(self, relpath):
        yield

    async def request_definition(self, relpath, line, col):
        self.calls.append(("def", relpath, line, col))
        return [{"uri": (self.root / "target.py").as_uri(),
                 "range": {"start": {"line": 9, "character": 4}}}]

    async def request_references(self, relpath, line, col):
        self.calls.append(("ref", relpath, line, col))
        return [{"uri": (self.root / "a.py").as_uri(), "range": {"start": {"line": 0, "character": 0}}},
                {"uri": (self.root / "b.py").as_uri(), "range": {"start": {"line": 2, "character": 1}}}]

    async def request_hover(self, relpath, line, col):
        self.calls.append(("hov", relpath, line, col))
        return {"contents": {"value": "def foo() -> int"}}

    async def request_document_symbols(self, relpath):
        self.calls.append(("doc", relpath))
        return [{"name": "foo", "kind": 12, "range": {"start": {"line": 3, "character": 0}}}]

    async def request_workspace_symbol(self, query):
        self.calls.append(("ws", query))
        return [{"name": "foo", "kind": 12,
                 "location": {"uri": (self.root / "a.py").as_uri(),
                              "range": {"start": {"line": 3, "character": 0}}}}]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _manager(tmp_path, fake_holder):
    def factory(language, root):
        srv = _FakeServer(root)
        fake_holder.append(srv)
        return srv
    return LspManager(tmp_path, server_factory=factory)


@pytest.mark.anyio
async def test_goto_definition_translates_coordinates_and_formats(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    fakes: list = []
    mgr = _manager(tmp_path, fakes)
    out = await mgr.goto_definition("m.py", 10, 5)  # 1-based in
    assert fakes[0].calls == [("def", "m.py", 9, 4)]  # 0-based out
    assert "target.py:10:5" in out  # formatted back to 1-based
    await mgr.aclose()


@pytest.mark.anyio
async def test_find_references_lists_all(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    fakes: list = []
    mgr = _manager(tmp_path, fakes)
    out = await mgr.find_references("m.py", 1, 1)
    assert "a.py:1:1" in out and "b.py:3:2" in out
    await mgr.aclose()


@pytest.mark.anyio
async def test_server_started_once_and_reused(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    fakes: list = []
    mgr = _manager(tmp_path, fakes)
    await mgr.goto_definition("m.py", 1, 1)
    await mgr.find_references("m.py", 1, 1)
    assert len(fakes) == 1 and fakes[0].started == 1
    await mgr.aclose()


@pytest.mark.anyio
async def test_unsupported_filetype(tmp_path):
    (tmp_path / "x.md").write_text("# hi\n")
    mgr = _manager(tmp_path, [])
    out = await mgr.goto_definition("x.md", 1, 1)
    assert "no language server" in out.lower() or "unsupported" in out.lower()
    await mgr.aclose()


@pytest.mark.anyio
async def test_disabled_language(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    mgr = LspManager(tmp_path, disabled=frozenset({"python"}),
                     server_factory=lambda lang, root: None)
    out = await mgr.goto_definition("m.py", 1, 1)
    assert "disabled" in out.lower()
    await mgr.aclose()


@pytest.mark.anyio
async def test_hover_and_document_symbols(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    fakes: list = []
    mgr = _manager(tmp_path, fakes)
    hov = await mgr.hover("m.py", 1, 1)
    assert "foo" in hov
    syms = await mgr.document_symbols("m.py")
    assert "foo" in syms and ":4" in syms  # 1-based line
    await mgr.aclose()


@pytest.mark.anyio
async def test_request_timeout_degrades(tmp_path):
    import asyncio

    class _Slow(_FakeServer):
        async def request_definition(self, relpath, line, col):
            await asyncio.sleep(5)

    (tmp_path / "m.py").write_text("x = 1\n")
    mgr = LspManager(tmp_path, request_timeout=0.05,
                     server_factory=lambda lang, root: _Slow(tmp_path))
    out = await mgr.goto_definition("m.py", 1, 1)
    assert "timed out" in out.lower()
    await mgr.aclose()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_lsp_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.lsp.manager'`

- [ ] **Step 4: Implement the manager**

```python
# src/marim_harness/lsp/manager.py
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
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

from . import registry
from .diagnostics import DiagnosticsCollector, format_diagnostics

logger = logging.getLogger(__name__)

_MAX_RESULTS = 50


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
        server_factory: Optional[Callable[[str, Path], object]] = None,
        request_timeout: float = 15.0,
        start_timeout: float = 60.0,
    ) -> None:
        self.root = root
        self._disabled = disabled
        self._factory = server_factory or _default_factory
        self._request_timeout = request_timeout
        self._start_timeout = start_timeout
        self._stack = AsyncExitStack()
        self._servers: dict[str, object] = {}
        self._collectors: dict[str, DiagnosticsCollector] = {}
        self._lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------------

    async def _server_for(self, path: str) -> tuple[Optional[object], Optional[str], Optional[str]]:
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
        async with self._lock:
            if language in self._servers:  # double-checked after acquiring
                return self._servers[language], language, None
            try:
                server = self._factory(language, self.root)
                await asyncio.wait_for(
                    self._stack.enter_async_context(server.start_server()),
                    timeout=self._start_timeout,
                )
            except Exception as exc:  # noqa: BLE001 — degrade to a message
                logger.debug("failed to start %s server: %s", language, exc)
                return None, language, f"Could not start the {language} language server: {exc}"
            collector = DiagnosticsCollector()
            collector.attach(server)
            self._servers[language] = server
            self._collectors[language] = collector
            return server, language, None

    async def aclose(self) -> None:
        """Shut down every started language server. Safe to call when none ran."""
        try:
            await self._stack.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.debug("error during LSP shutdown: %s", exc)
        self._servers.clear()
        self._collectors.clear()

    # --- helpers -------------------------------------------------------------

    async def _call(self, coro, what: str) -> tuple[Optional[object], Optional[str]]:
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
            uri = loc.get("uri") or loc.get("absolutePath") or ""
            start = loc.get("range", {}).get("start", {})
            rel = _uri_to_rel(self.root, uri) if uri.startswith("file:") else (uri or "<unknown>")
            out.append(f"{rel}:{start.get('line', 0) + 1}:{start.get('character', 0) + 1}")
        extra = len(items) - _MAX_RESULTS
        if extra > 0:
            out.append(f"… and {extra} more")
        return "\n".join(out)

    # --- operations ----------------------------------------------------------

    async def goto_definition(self, path: str, line: int, col: int) -> str:
        server, _lang, err = await self._server_for(path)
        if err:
            return err
        res, err = await self._call(
            server.request_definition(path, line - 1, col - 1), "goto_definition"
        )
        return err or self._format_locations("definitions", res)

    async def find_references(self, path: str, line: int, col: int) -> str:
        server, _lang, err = await self._server_for(path)
        if err:
            return err
        res, err = await self._call(
            server.request_references(path, line - 1, col - 1), "find_references"
        )
        return err or self._format_locations("references", res)

    async def hover(self, path: str, line: int, col: int) -> str:
        server, _lang, err = await self._server_for(path)
        if err:
            return err
        res, err = await self._call(
            server.request_hover(path, line - 1, col - 1), "hover"
        )
        if err:
            return err
        return _hover_text(res) or "No hover information."

    async def document_symbols(self, path: str) -> str:
        server, _lang, err = await self._server_for(path)
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
        for language, server in self._servers.items():
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
        Mirrors _server_for's start path; failures are swallowed (best-effort)."""
        if language in self._servers:
            return
        async with self._lock:
            if language in self._servers:
                return
            try:
                server = self._factory(language, self.root)
                await asyncio.wait_for(
                    self._stack.enter_async_context(server.start_server()),
                    timeout=self._start_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("failed to start %s server: %s", language, exc)
                return
            collector = DiagnosticsCollector()
            collector.attach(server)
            self._servers[language] = server
            self._collectors[language] = collector

    async def diagnostics(self, path: str, *, settle: float = 1.5) -> str:
        server, language, err = await self._server_for(path)
        if err:
            return err
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_lsp_manager.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check src/marim_harness/lsp/manager.py tests/test_lsp_manager.py && uv run pyright src/marim_harness/lsp/manager.py`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/marim_harness/lsp/manager.py tests/test_lsp_manager.py
git commit -m "feat(lsp): LspManager — lazy server pool, navigation, diagnostics, timeouts"
```

---

### Task 4: Wire `lsp` onto `Deps`

**Files:**
- Modify: `src/marim_harness/deps.py`
- Test: `tests/test_deps.py`

**Interfaces:**
- Consumes: `LspManager` (TYPE_CHECKING-only import to avoid pulling the lsp package into the hot import path).
- Produces: `Deps.lsp: Optional["LspManager"] = None`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_deps.py
from pathlib import Path

from marim_harness.deps import Deps


def test_deps_lsp_defaults_to_none():
    d = Deps(workspace_root=Path("."))
    assert d.lsp is None


def test_deps_lsp_can_be_set():
    d = Deps(workspace_root=Path("."))
    sentinel = object()
    d.lsp = sentinel
    assert d.lsp is sentinel
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deps.py -k lsp -v`
Expected: FAIL — `AttributeError: 'Deps' object has no attribute 'lsp'`

- [ ] **Step 3: Add the field**

In `src/marim_harness/deps.py`, add to the `TYPE_CHECKING` block (after the `HookRunner` import):

```python
if TYPE_CHECKING:
    from .hooks.runner import HookRunner
    from .lsp.manager import LspManager
```

Then add the field to the `Deps` dataclass, immediately after the `hooks` field:

```python
    # Optional session-scoped LSP server pool. None when no LSP is wired (every
    # LSP tool becomes a cheap ``is None`` guard returning an unavailable note).
    lsp: Optional["LspManager"] = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_deps.py -k lsp -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Type-check (forward ref resolves)**

Run: `uv run pyright src/marim_harness/deps.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/deps.py tests/test_deps.py
git commit -m "feat(lsp): add optional lsp manager handle to Deps"
```

---

### Task 5: LSP tools + registration

**Files:**
- Modify: `src/marim_harness/tools/names.py`
- Modify: `src/marim_harness/tools/provider.py`
- Test: `tests/test_lsp_tools.py`

**Interfaces:**
- Consumes: `Deps.lsp` (the `LspManager` API from Task 3).
- Produces: tool functions `goto_definition`, `find_references`, `hover`, `document_symbols`, `workspace_symbols`, `diagnostics` registered on the main agent (un-gated) and available to subagents; `names.LSP_TOOLS` folded into `READ_TOOLS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lsp_tools.py
from pathlib import Path

import pytest

from marim_harness.deps import Deps
from marim_harness.tools import names, provider


class _FakeLsp:
    def __init__(self):
        self.calls = []

    async def goto_definition(self, path, line, col):
        self.calls.append(("def", path, line, col))
        return "target.py:10:5"

    async def find_references(self, path, line, col):
        self.calls.append(("ref", path, line, col))
        return "a.py:1:1"

    async def hover(self, path, line, col):
        return "def foo() -> int"

    async def document_symbols(self, path):
        return "function foo  :4"

    async def workspace_symbols(self, query):
        return f"foo  a.py:4 ({query})"

    async def diagnostics(self, path, *, settle=1.5):
        return f"{path}: no diagnostics"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Ctx:
    def __init__(self, deps):
        self.deps = deps


@pytest.mark.anyio
async def test_goto_definition_tool_delegates(tmp_path):
    lsp = _FakeLsp()
    ctx = _Ctx(Deps(workspace_root=tmp_path, lsp=lsp))
    out = await provider.goto_definition(ctx, "m.py", 10, 5)
    assert out == "target.py:10:5"
    assert lsp.calls == [("def", "m.py", 10, 5)]


@pytest.mark.anyio
async def test_tools_report_unavailable_without_lsp(tmp_path):
    ctx = _Ctx(Deps(workspace_root=tmp_path, lsp=None))
    out = await provider.find_references(ctx, "m.py", 1, 1)
    assert "not available" in out.lower()


@pytest.mark.anyio
async def test_diagnostics_tool_delegates(tmp_path):
    ctx = _Ctx(Deps(workspace_root=tmp_path, lsp=_FakeLsp()))
    out = await provider.diagnostics(ctx, "m.py")
    assert "no diagnostics" in out


def test_lsp_tool_names_are_read_tools():
    expected = {"goto_definition", "find_references", "hover",
                "document_symbols", "workspace_symbols", "diagnostics"}
    assert expected <= names.LSP_TOOLS
    assert expected <= names.READ_TOOLS  # subagents granted read get LSP too


def test_lsp_tools_registered_on_main_agent(tmp_path):
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    agent = Agent(TestModel(), deps_type=Deps)
    provider.BuiltinToolProvider().register(agent)
    with agent.override(model=TestModel(call_tools=[])):
        result = agent.run_sync("hi", deps=Deps(workspace_root=tmp_path))
    assert result is not None  # smoke: registration doesn't break agent build


def test_lsp_tools_in_subagent_fns():
    for name in ("goto_definition", "find_references", "hover",
                 "document_symbols", "workspace_symbols", "diagnostics"):
        assert name in provider._SUBAGENT_FNS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lsp_tools.py -v`
Expected: FAIL — `AttributeError: module 'marim_harness.tools.provider' has no attribute 'goto_definition'`

- [ ] **Step 3: Add tool names**

In `src/marim_harness/tools/names.py`, add `LSP_TOOLS` and fold it into `READ_TOOLS`:

```python
READ_TOOLS = frozenset({"read_file", "glob", "tree", "grep"}) | frozenset({
    "goto_definition", "find_references", "hover",
    "document_symbols", "workspace_symbols", "diagnostics",
})
LSP_TOOLS = frozenset({
    "goto_definition", "find_references", "hover",
    "document_symbols", "workspace_symbols", "diagnostics",
})
```

(Keep `NET_TOOLS`, `GATED_TOOLS`, `SUBAGENT_TOOLS` lines unchanged below.)

- [ ] **Step 4: Add the tool functions**

In `src/marim_harness/tools/provider.py`, add these six functions (place them after `grep` and before `remember`):

```python
_LSP_UNAVAILABLE = "LSP is not available in this session."


async def goto_definition(ctx: RunContext[Deps], path: str, line: int, col: int) -> str:
    """Jump to where the symbol at `path:line:col` is defined, returning the
    target location(s) as `path:line:col`. Coordinates are 1-based — read them
    off `read_file`/`grep` output. Prefer this over grepping for a definition."""
    if ctx.deps.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.lsp.goto_definition(path, line, col)


async def find_references(ctx: RunContext[Deps], path: str, line: int, col: int) -> str:
    """List every use of the symbol at `path:line:col` across the project, as
    `path:line:col` lines. Coordinates are 1-based. Use before renaming or
    removing a symbol to see its blast radius."""
    if ctx.deps.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.lsp.find_references(path, line, col)


async def hover(ctx: RunContext[Deps], path: str, line: int, col: int) -> str:
    """Show the type/signature and docs for the symbol at `path:line:col`
    (1-based), as the language server's hover text. Use to learn a value's type
    without opening its definition."""
    if ctx.deps.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.lsp.hover(path, line, col)


async def document_symbols(ctx: RunContext[Deps], path: str) -> str:
    """Outline one file: its classes, functions, and methods with line numbers.
    A fast way to understand a file's shape before reading it in full."""
    if ctx.deps.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.lsp.document_symbols(path)


async def workspace_symbols(ctx: RunContext[Deps], query: str) -> str:
    """Find a symbol by name across the whole project, returning matches as
    `name  path:line`. Use to locate a class/function when you know its name but
    not its file."""
    if ctx.deps.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.lsp.workspace_symbols(query)


async def diagnostics(ctx: RunContext[Deps], path: str) -> str:
    """Report the language server's errors and warnings for `path`, as
    `path:line:col: severity: message`. Edits already append fresh diagnostics
    automatically; call this to re-check a file on demand."""
    if ctx.deps.lsp is None:
        return _LSP_UNAVAILABLE
    return await ctx.deps.lsp.diagnostics(path)
```

- [ ] **Step 5: Register on the main agent and for subagents**

In `BuiltinToolProvider.register`, add after `agent.tool(grep)`:

```python
        agent.tool(goto_definition)
        agent.tool(find_references)
        agent.tool(hover)
        agent.tool(document_symbols)
        agent.tool(workspace_symbols)
        agent.tool(diagnostics)
```

In the `_SUBAGENT_FNS` dict, add these entries (after `"grep": grep,`):

```python
    "goto_definition": goto_definition,
    "find_references": find_references,
    "hover": hover,
    "document_symbols": document_symbols,
    "workspace_symbols": workspace_symbols,
    "diagnostics": diagnostics,
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_lsp_tools.py -v`
Expected: PASS (6 tests)

- [ ] **Step 7: Run the existing provider/subagent suites (no regressions)**

Run: `uv run pytest tests/test_provider.py tests/test_subagent_tool.py tests/test_imports.py -v`
Expected: PASS

- [ ] **Step 8: Lint and type-check**

Run: `uv run ruff check src/marim_harness/tools tests/test_lsp_tools.py && uv run pyright src/marim_harness/tools/provider.py`
Expected: no errors

- [ ] **Step 9: Commit**

```bash
git add src/marim_harness/tools/names.py src/marim_harness/tools/provider.py tests/test_lsp_tools.py
git commit -m "feat(lsp): six navigation/diagnostics tools, wired for main agent and subagents"
```

---

### Task 6: Diagnostics-on-edit

**Files:**
- Modify: `src/marim_harness/tools/provider.py` (the `write_file` and `edit_file` tool wrappers)
- Test: `tests/test_lsp_tools.py` (extend)

**Interfaces:**
- Consumes: `Deps.lsp.diagnostics(path, settle=...)`; `fs.write_file`/`fs.edit_file` (unchanged).
- Produces: `write_file`/`edit_file` tool results gain a trailing diagnostics block when `deps.lsp` is set and finds problems; unchanged when `lsp is None`.

A short module-level helper appends best-effort diagnostics so both tools share one code path. The settle window is short here (0.8s) and bounded by the manager's own request timeout — a slow server can't stall an edit.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_lsp_tools.py

class _DiagLsp:
    def __init__(self, report):
        self.report = report
        self.seen = []

    async def diagnostics(self, path, *, settle=1.5):
        self.seen.append((path, settle))
        return self.report


@pytest.mark.anyio
async def test_edit_appends_diagnostics(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("x = 1\n")
    lsp = _DiagLsp("m.py:1:1: error: bad")
    ctx = _Ctx(Deps(workspace_root=tmp_path, lsp=lsp))
    from marim_harness.tools import fs
    out = await provider.edit_file(ctx, "m.py", [fs.Edit(old_string="x = 1", new_string="y = 2")])
    assert "edited m.py" in out
    assert "m.py:1:1: error: bad" in out
    assert lsp.seen and lsp.seen[0][0] == "m.py"


@pytest.mark.anyio
async def test_write_appends_diagnostics(tmp_path):
    lsp = _DiagLsp("n.py:2:3: warning: meh")
    ctx = _Ctx(Deps(workspace_root=tmp_path, lsp=lsp))
    out = await provider.write_file(ctx, "n.py", "z = 3\n")
    assert "wrote n.py" in out
    assert "n.py:2:3: warning: meh" in out


@pytest.mark.anyio
async def test_edit_no_diagnostics_block_when_clean(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("x = 1\n")
    lsp = _DiagLsp("m.py: no diagnostics")
    ctx = _Ctx(Deps(workspace_root=tmp_path, lsp=lsp))
    from marim_harness.tools import fs
    out = await provider.edit_file(ctx, "m.py", [fs.Edit(old_string="x = 1", new_string="y = 2")])
    # A clean file adds no noise.
    assert "no diagnostics" not in out
    assert out.strip().endswith("edit)")


@pytest.mark.anyio
async def test_write_without_lsp_is_unchanged(tmp_path):
    ctx = _Ctx(Deps(workspace_root=tmp_path, lsp=None))
    out = await provider.write_file(ctx, "n.py", "z = 3\n")
    assert out == "wrote n.py (6 bytes)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_lsp_tools.py -k "diagnostics or unchanged or clean" -v`
Expected: FAIL — diagnostics not appended to edit/write results

- [ ] **Step 3: Implement the shared helper and wire it in**

In `src/marim_harness/tools/provider.py`, add a helper near the other module-level functions (e.g. just before `write_file`):

```python
async def _with_diagnostics(ctx: RunContext[Deps], path: str, result: str) -> str:
    """Append best-effort LSP diagnostics for ``path`` to a write/edit ``result``.

    No-op when no LSP is wired, when the language isn't served, or when the file
    is clean — so a successful edit only grows output when there's something the
    model should fix. Never raises: any failure leaves ``result`` untouched."""
    if ctx.deps.lsp is None:
        return result
    try:
        report = await ctx.deps.lsp.diagnostics(path, settle=0.8)
    except Exception:  # noqa: BLE001 — diagnostics must never fail an edit
        return result
    low = report.lower()
    if not report or "no diagnostics" in low or "unavailable" in low \
            or "no language server" in low or "disabled" in low:
        return result
    return f"{result}\n\ndiagnostics:\n{report}"
```

Then change `write_file` and `edit_file` to async and route through it:

```python
async def write_file(ctx: RunContext[Deps], path: str, content: str) -> str:
    """Create or overwrite a file. `path` is relative to the workspace root."""
    result = fs.write_file(ctx.deps.workspace_root, path, content)
    return await _with_diagnostics(ctx, path, result)


async def edit_file(ctx: RunContext[Deps], path: str, edits: list[fs.Edit]) -> str:
    """Apply one or more find/replace edits to a file, in order and
    all-or-nothing. Each edit is {old_string, new_string, replace_all?};
    old_string must match exactly once unless replace_all is set."""
    result = fs.edit_file(ctx.deps.workspace_root, path, edits)
    return await _with_diagnostics(ctx, path, result)
```

(`write_file`/`edit_file` are already registered with `requires_approval=True` and added to `_SUBAGENT_FNS`; making them async changes nothing about registration — pydantic-ai supports async tools, as `bash`/`fetch_url` already are.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_lsp_tools.py -v`
Expected: PASS (all, including the 4 new)

- [ ] **Step 5: Run the fs/provider suites (no regressions from async change)**

Run: `uv run pytest tests/test_fs.py tests/test_provider.py -v`
Expected: PASS

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check src/marim_harness/tools/provider.py && uv run pyright src/marim_harness/tools/provider.py`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/tools/provider.py tests/test_lsp_tools.py
git commit -m "feat(lsp): append best-effort diagnostics after write_file/edit_file"
```

---

### Task 7: Harness wiring and teardown

**Files:**
- Modify: `src/marim_harness/agent.py` (Harness `__init__` and `aclose`)
- Test: `tests/test_agent.py` (extend)

**Interfaces:**
- Consumes: `LspManager` (Task 3); `Deps.lsp` (Task 4); `aclose()` teardown point (existing, line ~279).
- Produces: a live `LspManager` on `self.lsp`, assigned to `self.deps.lsp`, shut down in `aclose()`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_agent.py
from pathlib import Path

import pytest

from marim_harness.lsp.manager import LspManager


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _minimal_harness(tmp_path: Path):
    """Build a Harness with the simplest valid wiring for lifecycle tests."""
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.deps import Deps
    from marim_harness.tools.provider import BuiltinToolProvider

    return Harness(
        TestModel(),
        BuiltinToolProvider(),
        Deps(workspace_root=tmp_path),
        instructions="test",
    )


def test_harness_wires_lsp_manager(tmp_path):
    h = _minimal_harness(tmp_path)
    assert isinstance(h.lsp, LspManager)
    assert h.deps.lsp is h.lsp


@pytest.mark.anyio
async def test_harness_aclose_shuts_down_lsp(tmp_path):
    h = _minimal_harness(tmp_path)
    closed = {"n": 0}

    async def fake_aclose():
        closed["n"] += 1

    h.lsp.aclose = fake_aclose  # type: ignore[method-assign]
    await h.aclose()
    assert closed["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent.py -k lsp -v`
Expected: FAIL — `AttributeError: 'Harness' object has no attribute 'lsp'`

- [ ] **Step 3: Construct the manager in `__init__`**

In `src/marim_harness/agent.py`, add the import near the other local imports at the top (with `from .deps import ...`):

```python
from .lsp.manager import LspManager
```

In `Harness.__init__`, after `self.deps = deps` (line ~177), add:

```python
        # Session-scoped LSP server pool, reachable by the navigation/diagnostics
        # tools through deps. Subagents share this deps object, so they get LSP too.
        self.lsp = LspManager(deps.workspace_root)
        self.deps.lsp = self.lsp
```

- [ ] **Step 4: Tear it down in `aclose`**

Change `aclose` (line ~279) from:

```python
    async def aclose(self) -> None:
        await self.mcp.aclose()
```

to:

```python
    async def aclose(self) -> None:
        await self.mcp.aclose()
        await self.lsp.aclose()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent.py -k lsp -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run the full agent suite (no regressions)**

Run: `uv run pytest tests/test_agent.py -v`
Expected: PASS

- [ ] **Step 7: Lint and type-check**

Run: `uv run ruff check src/marim_harness/agent.py && uv run pyright src/marim_harness/agent.py`
Expected: no errors

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/agent.py tests/test_agent.py
git commit -m "feat(lsp): construct LspManager on Harness, wire onto deps, close on teardown"
```

---

### Task 8: Optional pyright integration test

**Files:**
- Create: `tests/test_lsp_integration.py`

**Interfaces:**
- Consumes: the real `LspManager` against a real `multilspy` Python server. Skipped unless a Python server is locally available.

This is the one test that exercises real multilspy end-to-end, validating the parts the unit tests mock (real return shapes, real diagnostics push). It is skipped when no Python language server is installed, so CI without pyright stays green.

- [ ] **Step 1: Write the integration test**

```python
# tests/test_lsp_integration.py
import pytest

from marim_harness.lsp import registry
from marim_harness.lsp.manager import LspManager

pytestmark = pytest.mark.skipif(
    "python" not in registry.locally_installed_languages(),
    reason="no local Python language server (pyright) installed",
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_definition_and_diagnostics_real_pyright(tmp_path):
    (tmp_path / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "main.py").write_text("from lib import add\n\nadd(1, 2)\n")
    mgr = LspManager(tmp_path, start_timeout=120.0, request_timeout=60.0)
    try:
        # goto_definition on `add` usage (main.py line 3, col 1, 1-based) → lib.py
        defn = await mgr.goto_definition("main.py", 3, 1)
        assert "lib.py" in defn
        # diagnostics on a file with an undefined name → at least one error
        (tmp_path / "bad.py").write_text("print(undefined_name)\n")
        diag = await mgr.diagnostics("bad.py", settle=3.0)
        assert "bad.py" in diag
    finally:
        await mgr.aclose()
```

- [ ] **Step 2: Run it (or confirm skip)**

Run: `uv run pytest tests/test_lsp_integration.py -v`
Expected: PASS if a Python server is installed, otherwise SKIPPED with the reason. If it errors instead of passing/skipping, the real multilspy return shapes differ from the unit-test fakes — adjust `LspManager`'s parsing helpers (`_format_locations`, `_format_symbols`, `_hover_text`) to match, keeping the unit tests green.

- [ ] **Step 3: Run the whole suite + lint + types**

Run: `uv run pytest -q && uv run ruff check && uv run pyright`
Expected: all pass (integration test passes or skips)

- [ ] **Step 4: Commit**

```bash
git add tests/test_lsp_integration.py
git commit -m "test(lsp): optional real-pyright integration test for definition + diagnostics"
```

---

## Self-Review

**Spec coverage:**
- Navigation tools (definition/references/hover/document & workspace symbols) → Tasks 3 (manager ops) + 5 (tools). ✓
- Diagnostics on demand → Task 5 (`diagnostics` tool) + Task 3 (`manager.diagnostics`). ✓
- Diagnostics on edit → Task 6. ✓
- multilspy client layer → Task 3 (`_default_factory`, dependency add). ✓
- Registry (extension→language, availability, install hints, enable/disable) → Task 1. ✓
- `LspManager` lifecycle (lazy start, hold open, timeout guard, clean shutdown) → Tasks 3 + 7. ✓
- Coordinate translation (1-based↔0-based) → Task 3 (verified by `test_goto_definition_translates_coordinates`). ✓
- `Deps.lsp` optional handle, `None` = no-op → Task 4; no-op paths tested in Tasks 5 & 6. ✓
- Read-only tools in read set + subagent reach → Task 5. ✓
- Testing strategy (unit no-server, wiring with fakes, optional pyright integration) → Tasks 1–8. ✓
- **Deviation from spec:** the spec lists "override its server command" as an optional config knob. Upstream `multilspy` selects its own server per language and exposes no command-override hook, so this is dropped (only enable/disable via `disabled` is implemented). Switching to `solidlsp` later would be the path to add it. The `disabled` set is plumbed through `LspManager` but not yet sourced from a config file — wiring it to harness config is a trivial follow-up and not required for the feature to work.

**Placeholder scan:** No TBD/TODO/"handle edge cases" left; every code step contains full code; every test step contains real assertions.

**Type consistency:** `LspManager` method names (`goto_definition`, `find_references`, `hover`, `document_symbols`, `workspace_symbols`, `diagnostics`, `aclose`) are identical across Tasks 3, 5, 6, 7, 8 and the tool wrappers. `Deps.lsp` is used consistently. `_with_diagnostics` and `_SUBAGENT_FNS` names match across Tasks 5–6. Tool names in `names.LSP_TOOLS`/`READ_TOOLS` match the registered function names and the `_SUBAGENT_FNS` keys.
