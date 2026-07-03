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
        return [
            {"uri": (self.root / "a.py").as_uri(), "range": {"start": {"line": 0, "character": 0}}},
            {"uri": (self.root / "b.py").as_uri(), "range": {"start": {"line": 2, "character": 1}}},
        ]

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


def test_format_locations_tolerates_non_string_uri(tmp_path):
    """A misbehaving server can hand back a non-string uri; formatting it must
    not raise AttributeError (uri.startswith) into the tool."""
    mgr = LspManager(tmp_path)
    out = mgr._format_locations(
        "definitions",
        [{"uri": 123, "range": {"start": {"line": 0, "character": 0}}}],
    )
    assert "1:1" in out  # did not raise; degraded gracefully


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
async def test_diagnostics_honor_disabled_python(tmp_path, monkeypatch):
    """Python diagnostics bypass the LSP server and shell out to ruff/pyright —
    but that shortcut must still honor the per-language disable switch, like
    every other LSP operation. Disabled means no subprocesses run."""
    from marim_harness.lsp import checks

    (tmp_path / "m.py").write_text("x = 1\n")
    ran: list = []

    async def spy_run(cmd, cwd, timeout):
        ran.append(cmd)
        return "[]"

    monkeypatch.setattr(checks, "_run", spy_run)
    mgr = LspManager(tmp_path, disabled=frozenset({"python"}),
                     server_factory=lambda lang, root: None)
    out = await mgr.diagnostics("m.py")
    assert "disabled" in out.lower()
    assert ran == [], "external checkers ran despite python LSP being disabled"
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
async def test_workspace_symbols_aggregates(tmp_path):
    """workspace_symbols returns aggregated, 1-based-line formatted results.

    To ensure a server is already in the pool (avoiding dependency on
    locally_installed_languages()), we first trigger a server via goto_definition,
    then call workspace_symbols. The fake returns a symbol named "foo" at
    range.start.line == 3, so the formatted output must contain "a.py:4".
    """
    (tmp_path / "m.py").write_text("x = 1\n")
    fakes: list = []
    mgr = _manager(tmp_path, fakes)
    # Seed the pool with a running server.
    await mgr.goto_definition("m.py", 1, 1)
    assert len(mgr._servers) == 1, "server should be in pool after goto_definition"

    out = await mgr.workspace_symbols("foo")
    assert "foo" in out
    assert "a.py:4" in out  # line 3 (0-based) → 4 (1-based)
    await mgr.aclose()


@pytest.mark.anyio
async def test_diagnostics_no_diagnostics_pushed(tmp_path):
    """With the fake server's on_notification accepting registration without error,
    collector.attach succeeds (enabled=True), but no diagnostics are ever pushed.
    diagnostics() should return the 'no diagnostics' string from format_diagnostics.
    """
    (tmp_path / "m.py").write_text("x = 1\n")
    fakes: list = []
    mgr = _manager(tmp_path, fakes)
    out = await mgr.diagnostics("m.py")
    assert "no diagnostics" in out
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


@pytest.mark.anyio
async def test_concurrent_same_language_starts_once(tmp_path):
    """Concurrent operations on one language share a single startup: the server is
    created and started exactly once, never one-per-caller (fan-out safe)."""
    import asyncio

    fakes: list = []

    class _Slow(_FakeServer):
        @contextlib.asynccontextmanager
        async def start_server(self):
            self.started += 1
            await asyncio.sleep(0.05)  # widen the race window between callers
            yield self

    def factory(language, root):
        srv = _Slow(root)
        fakes.append(srv)
        return srv

    (tmp_path / "m.py").write_text("x = 1\n")
    mgr = LspManager(tmp_path, server_factory=factory)
    outs = await asyncio.gather(*[mgr.goto_definition("m.py", 1, 1) for _ in range(6)])
    assert all("target.py" in o for o in outs)
    assert len(fakes) == 1 and fakes[0].started == 1
    await mgr.aclose()


@pytest.mark.anyio
async def test_startup_does_not_block_across_languages(tmp_path, monkeypatch):
    """A slow startup for one language must not block operations on a different
    language — fan-out across languages can't serialize on a shared lock."""
    import asyncio

    from marim_harness.lsp import registry

    monkeypatch.setattr(
        registry, "availability", lambda lang: registry.Availability(True, "")
    )

    py_in_start = asyncio.Event()  # signals the python startup has begun
    py_release = asyncio.Event()  # set by the test to let the slow start finish

    class _Gated(_FakeServer):
        def __init__(self, root, language):
            super().__init__(root)
            self.language = language

        @contextlib.asynccontextmanager
        async def start_server(self):
            self.started += 1
            if self.language == "python":
                py_in_start.set()
                await py_release.wait()  # hang the python startup
            yield self

    mgr = LspManager(tmp_path, server_factory=lambda lang, root: _Gated(root, lang))
    (tmp_path / "m.py").write_text("x = 1\n")
    (tmp_path / "m.ts").write_text("const x = 1\n")

    py_task = asyncio.create_task(mgr.goto_definition("m.py", 1, 1))
    await py_in_start.wait()  # python startup is in flight (would hold any lock)

    # typescript must complete even while the python startup is stuck.
    ts_out = await asyncio.wait_for(mgr.goto_definition("m.ts", 1, 1), timeout=2)
    assert "target.py" in ts_out

    py_release.set()  # release the python startup so its task can finish
    await py_task
    await mgr.aclose()


def test_clamp_hover_caps_wide_blobs():
    from marim_harness.lsp.manager import _MAX_HOVER_CHARS, _clamp_hover

    short = "def foo() -> int"
    assert _clamp_hover(short) == short  # under the cap: unchanged

    wide = "x" * (_MAX_HOVER_CHARS + 500)
    out = _clamp_hover(wide)
    assert len(out) < len(wide)
    assert out.startswith("x" * 100)  # head preserved
    assert "500 more chars truncated" in out


@pytest.mark.anyio
async def test_hover_clamps_huge_docstring(tmp_path):
    from marim_harness.lsp.manager import _MAX_HOVER_CHARS

    class _Huge(_FakeServer):
        async def request_hover(self, relpath, line, col):
            return {"contents": {"value": "S" * (_MAX_HOVER_CHARS + 9000)}}

    (tmp_path / "m.py").write_text("x = 1\n")
    mgr = LspManager(tmp_path, server_factory=lambda lang, root: _Huge(tmp_path))
    hov = await mgr.hover("m.py", 1, 1)
    assert len(hov) <= _MAX_HOVER_CHARS + 100  # cap + short footer
    assert "truncated" in hov
    await mgr.aclose()


@pytest.mark.anyio
async def test_diagnostics_wakes_on_publish_before_settle(tmp_path):
    """The non-Python diagnostics push path must return the moment the server
    publishes for the opened URI, treating ``settle`` as a ceiling rather than a
    fixed sleep. The fake publishes a diagnostic on didOpen via the registered
    notification handler; diagnostics() must surface it and return well under a
    large settle window."""
    import asyncio

    from marim_harness.lsp.manager import _path_to_uri

    class _PublishServer(_FakeServer):
        @contextlib.contextmanager
        def open_file(self, relpath):
            # didOpen → schedule a publishDiagnostics for this URI. ensure_future
            # runs the (async) handler once diagnostics() yields to the loop on its
            # wait_for, exactly as the real LSP endpoint delivers the notification.
            uri = _path_to_uri(self.root, relpath)
            rng = {"start": {"line": 4, "character": 2}}
            params = {
                "uri": uri,
                "diagnostics": [{"severity": 1, "message": "boom", "range": rng}],
            }
            cb = self.server.handlers.get("textDocument/publishDiagnostics")
            if cb is not None:
                asyncio.ensure_future(cb(params))
            yield

    class _PublishServerFactory(_PublishServer):
        def __init__(self, root):
            super().__init__(root)

            class _H:
                def __init__(self):
                    self.handlers: dict = {}

                def on_notification(self, method, handler):
                    self.handlers[method] = handler

            self.server = _H()

    (tmp_path / "x.ts").write_text("let a = 1\n")
    srv = _PublishServerFactory(tmp_path)
    mgr = LspManager(tmp_path, server_factory=lambda lang, root: srv)
    # Warm-start the typescript server directly (bypasses the availability probe,
    # which would otherwise gate a cold start in CI without a real tsserver).
    await mgr._start_language("typescript")
    assert mgr._collectors["typescript"].enabled

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    out = await mgr.diagnostics("x.ts", settle=5.0)
    elapsed = loop.time() - t0

    assert "boom" in out  # the pushed diagnostic surfaced (collector stayed current)
    assert "x.ts:5:3" in out  # 0-based (4,2) → 1-based (5,3)
    assert elapsed < 2.0  # woke on the publish, did not sleep the 5s ceiling
    await mgr.aclose()
