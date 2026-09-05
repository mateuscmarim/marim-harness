"""codex/catalog.py: model/list -> ModelEntry, with the static fallback."""

from __future__ import annotations

import pytest

from marim_harness.codex.catalog import STATIC_MODELS, entries_from, list_codex_models
from marim_harness.codex.server import CodexServer
from tests.fakes import fake_codex_bin

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
