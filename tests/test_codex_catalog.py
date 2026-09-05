"""codex/catalog.py: model/list -> ModelEntry, with the static fallback."""

from __future__ import annotations

import pytest

from marim_harness.codex.catalog import STATIC_MODELS, entries_from, list_codex_models
from marim_harness.codex.server import CodexServer, close_shared_server, peek_shared_server
from tests.fakes import fake_codex_bin, read_request_log

pytestmark = pytest.mark.anyio


def test_entries_from_marks_thinking_and_keeps_order():
    raw = [
        {
            "id": "gpt-5.6-sol",
            "displayName": "GPT-5.6 Sol",
            "isDefault": True,
            "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}],
        },
        {"id": "gpt-5.4-mini", "displayName": "GPT-5.4 mini", "supportedReasoningEfforts": []},
    ]
    entries = entries_from(raw)
    assert [e.id for e in entries] == ["gpt-5.6-sol", "gpt-5.4-mini"]
    assert entries[0].name == "GPT-5.6 Sol" and entries[0].provider == "codex-cli"
    assert all(e.supports_thinking is True for e in entries)  # every Codex model takes effort
    assert entries[0].context_window is None


def test_static_fallback_has_six_models_with_thinking():
    assert len(STATIC_MODELS) == 6
    assert {e.provider for e in STATIC_MODELS} == {"codex-cli"}
    assert all(e.supports_thinking for e in STATIC_MODELS)


async def test_list_codex_models_live(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {}))
    try:
        entries = await list_codex_models(server=server)
    finally:
        await server.aclose()
    assert [e.id for e in entries] == ["gpt-5.6-sol", "gpt-5.4-mini"]


async def test_list_codex_models_falls_back_when_server_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CODEX_CLI_BIN", str(tmp_path / "missing"))
    server = CodexServer()
    entries = await list_codex_models(server=server)
    assert entries == list(STATIC_MODELS)
    with pytest.raises(Exception):  # noqa: B017 - CodexUnavailable/OSError, binary-resolution dependent
        await list_codex_models(server=server, strict=True)


async def test_list_codex_models_empty_live_response_falls_back_when_not_strict(tmp_path):
    # A live server that connects fine but reports zero models: non-strict
    # callers (the picker) still want a non-empty catalog to show.
    server = CodexServer(binary=fake_codex_bin(tmp_path, {"models": []}))
    try:
        entries = await list_codex_models(server=server)
    finally:
        await server.aclose()
    assert entries == list(STATIC_MODELS)


async def test_list_codex_models_with_no_server_given_starts_and_closes_its_own(
    tmp_path, monkeypatch
):
    """Final review Important #5: with no injected server AND no process-wide
    singleton already running (the `marim models list` / provider-detection
    shape), `list_codex_models` must not leak a live app-server — it starts
    its OWN private server, uses it, and closes it again, never touching (or
    creating) the shared singleton at all."""
    assert peek_shared_server() is None  # nothing else in this test has started codex-cli
    monkeypatch.setenv("MARIM_CODEX_CLI_BIN", fake_codex_bin(tmp_path, {}))
    closed: list[bool] = []
    orig_aclose = CodexServer.aclose

    async def spy_aclose(self):
        closed.append(True)
        await orig_aclose(self)

    monkeypatch.setattr(CodexServer, "aclose", spy_aclose)

    entries = await list_codex_models()
    assert [e.id for e in entries] == ["gpt-5.6-sol", "gpt-5.4-mini"]
    # No shared instance was created as a side effect...
    assert peek_shared_server() is None
    # ...even though a real (private) process WAS spawned, used, and reaped.
    assert any(r["method"] == "model/list" for r in read_request_log(tmp_path))
    assert closed == [True]


async def test_list_codex_models_reuses_an_already_running_shared_server(tmp_path, monkeypatch):
    """The other half of Important #5: when a shared singleton is ALREADY
    running (something else in this process depends on it), a catalog probe
    must reuse it rather than spin up a redundant second process — and must
    not be the one to close it."""
    from marim_harness.codex.server import shared_server

    monkeypatch.setenv("MARIM_CODEX_CLI_BIN", fake_codex_bin(tmp_path, {}))
    srv = shared_server()
    await srv.start()
    try:
        entries = await list_codex_models()
        assert [e.id for e in entries] == ["gpt-5.6-sol", "gpt-5.4-mini"]
        assert peek_shared_server() is srv and srv.alive  # reused, not closed
    finally:
        await close_shared_server()


async def test_list_codex_models_empty_live_response_is_not_masked_when_strict(tmp_path):
    # strict=True is how provider verification tells "connected, 0 models"
    # apart from "failed to connect" -- an empty live response must come back
    # as [], not silently substitute the static fallback.
    server = CodexServer(binary=fake_codex_bin(tmp_path, {"models": []}))
    try:
        entries = await list_codex_models(server=server, strict=True)
    finally:
        await server.aclose()
    assert entries == []
