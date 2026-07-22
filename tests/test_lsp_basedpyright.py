"""The basedpyright multilspy subclass and the python server-selection seam.

multilspy hard-wires python -> jedi-language-server; marim ships its own
BasedPyrightServer subclass and prefers it whenever basedpyright-langserver is
on PATH (jedi-language-server is in maintenance mode). These tests pin the
selection seam and the subclass's handshake surface without spawning a server
process — the real end-to-end path is covered by test_lsp_integration.py when
a server is locally installed.
"""

import pytest

from marim_harness.lsp import manager as manager_mod
from marim_harness.lsp import registry
from marim_harness.lsp.bundled import bundled_lsp_providers
from marim_harness.lsp.manager import LspManager
from marim_harness.lsp.provider import LspRegistry


def _bundled_reg():
    return LspRegistry(bundled_lsp_providers())


def test_python_available_via_basedpyright_alone(monkeypatch):
    monkeypatch.setattr(
        registry.shutil,
        "which",
        lambda b: "/x/basedpyright-langserver" if b == "basedpyright-langserver" else None,
    )
    assert _bundled_reg().availability("python").available is True


def test_python_hint_recommends_basedpyright_first(monkeypatch):
    monkeypatch.setattr(registry.shutil, "which", lambda b: None)
    avail = _bundled_reg().availability("python")
    assert avail.available is False
    assert "basedpyright" in avail.hint
    # basedpyright is the recommended (actively developed) option; jedi stays
    # a supported fallback, so the hint names it second.
    assert avail.hint.index("basedpyright") < avail.hint.index("jedi-language-server")


def test_factory_prefers_basedpyright_when_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        manager_mod.shutil,
        "which",
        lambda b: "/x/basedpyright-langserver" if b == "basedpyright-langserver" else None,
    )
    from marim_harness.lsp.basedpyright import BasedPyrightServer

    mgr = LspManager(tmp_path, registry=_bundled_reg())
    server = mgr._default_factory("python", tmp_path)
    assert isinstance(server, BasedPyrightServer)


def test_factory_falls_back_to_jedi_without_basedpyright(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_mod.shutil, "which", lambda b: None)
    from multilspy.language_servers.jedi_language_server.jedi_server import JediServer

    mgr = LspManager(tmp_path, registry=_bundled_reg())
    server = mgr._default_factory("python", tmp_path)
    assert isinstance(server, JediServer)


def test_factory_other_languages_unchanged(monkeypatch, tmp_path):
    # The basedpyright preference is python-only: other languages keep
    # multilspy's own create() routing.
    monkeypatch.setattr(manager_mod.shutil, "which", lambda b: "/x/" + b)
    from multilspy.language_servers.typescript_language_server.typescript_language_server import (
        TypeScriptLanguageServer,
    )

    mgr = LspManager(tmp_path, registry=_bundled_reg())
    server = mgr._default_factory("typescript", tmp_path)
    assert isinstance(server, TypeScriptLanguageServer)


def _server(tmp_path):
    from multilspy.multilspy_config import MultilspyConfig
    from multilspy.multilspy_logger import MultilspyLogger

    from marim_harness.lsp.basedpyright import BasedPyrightServer

    return BasedPyrightServer(
        MultilspyConfig.from_dict({"code_language": "python"}),
        MultilspyLogger(),
        str(tmp_path),
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


_NULL_MSG = "Unexpected response from Language Server: None"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("method", "empty"),
    [
        ("request_definition", []),
        ("request_references", []),
        ("request_document_symbols", ([], None)),
    ],
)
async def test_null_response_normalizes_to_empty(monkeypatch, tmp_path, method, empty):
    """pyright legally answers `null` for no-result (LSP: Location[] | null);
    multilspy hard-asserts a list because jedi always answers []. The subclass
    normalizes only that case so the manager renders its ordinary
    'No X found.' message instead of the assert text."""
    from multilspy.language_server import LanguageServer

    async def null_assert(self, *a, **kw):
        raise AssertionError(_NULL_MSG)

    monkeypatch.setattr(LanguageServer, method, null_assert)
    server = _server(tmp_path)
    args = ("m.py",) if method == "request_document_symbols" else ("m.py", 0, 0)
    assert await getattr(server, method)(*args) == empty


@pytest.mark.anyio
async def test_genuinely_malformed_response_still_raises(monkeypatch, tmp_path):
    """Only the null case is normalized: a malformed non-null response keeps
    surfacing loudly rather than masquerading as 'no results'."""
    from multilspy.language_server import LanguageServer

    async def bad_assert(self, *a, **kw):
        raise AssertionError("Unexpected response from Language Server: {'weird': 1}")

    monkeypatch.setattr(LanguageServer, "request_references", bad_assert)
    server = _server(tmp_path)
    with pytest.raises(AssertionError):
        await server.request_references("m.py", 0, 0)


def test_basedpyright_initialize_params_point_at_the_workspace(tmp_path):
    server = _server(tmp_path)
    params = server._get_initialize_params(str(tmp_path))
    assert params["rootUri"] == tmp_path.as_uri()
    assert params["workspaceFolders"][0]["uri"] == tmp_path.as_uri()
    assert params["processId"]
    # The nav tools need these capabilities served; pyright checks the client
    # capabilities dict exists (contents can be minimal).
    assert "capabilities" in params
