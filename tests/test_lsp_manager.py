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
