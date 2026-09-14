"""claude/catalog.py: the CLI's ``initialize.models`` -> ModelEntry, the
handshake cache, the throwaway probe, and the static fallback."""

from __future__ import annotations

import pytest

from marim_harness.claude import catalog
from marim_harness.claude.catalog import (
    STATIC_MODELS,
    cached_models,
    entries_from,
    list_claude_models,
    remember,
)
from marim_harness.claude.process import ClaudeProcess, ProcessOptions
from marim_harness.config.model import ModelConfig, ModelSource
from tests.fakes import fake_claude_bin, read_claude_argvs, read_claude_log

pytestmark = pytest.mark.anyio

MENU = [
    {
        "value": "default",
        "resolvedModel": "claude-opus-5[1m]",
        "displayName": "Default (recommended)",
        "description": "Opus 5 with 1M context",
        "supportsEffort": True,
    },
    {"value": "opus", "resolvedModel": "claude-opus-5", "displayName": "Opus"},
    {"value": "sonnet", "resolvedModel": "claude-sonnet-5", "displayName": "Sonnet"},
    {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001", "displayName": "Haiku"},
]


@pytest.fixture(autouse=True)
def _fresh_cache():
    catalog.reset()
    yield
    catalog.reset()


# --- pure -------------------------------------------------------------------


def test_entries_from_keeps_cli_order_and_names_the_resolved_model():
    entries = entries_from(MENU)
    assert [e.id for e in entries] == ["default", "opus", "sonnet", "haiku"]
    assert entries[0].name == "Default (recommended) · claude-opus-5[1m]"
    assert entries[1].name == "Opus · claude-opus-5"
    assert {e.provider for e in entries} == {"claude-cli"}
    assert entries[0].qualified == "claude-cli:default"
    # marim's thinking level reaches Claude Code (budget + effort, one of
    # which every Claude model honours): every entry is annotated.
    assert all(e.supports_thinking is True for e in entries)


def test_entries_from_tolerates_partial_and_junk_rows():
    entries = entries_from(
        [
            {"value": "claude-fable-5-1[1m]", "resolvedModel": "claude-fable-5-1"},
            {"value": "opus", "resolvedModel": "opus"},  # alias == resolved: no suffix
            {"displayName": "no value"},
            "junk",  # type: ignore[list-item]
            {"value": ""},
        ]
    )
    assert [(e.id, e.name) for e in entries] == [
        ("claude-fable-5-1[1m]", "claude-fable-5-1[1m] · claude-fable-5-1"),
        ("opus", "opus"),
    ]


def test_static_fallback_is_the_family_aliases():
    assert [e.id for e in STATIC_MODELS] == ["opus", "sonnet", "haiku", "fable"]
    assert {e.provider for e in STATIC_MODELS} == {"claude-cli"}


# --- cache ------------------------------------------------------------------


def test_remember_ignores_responses_without_a_usable_menu():
    remember({"models": MENU}, binary="/bin/claude")
    assert [e.id for e in cached_models() or []] == ["default", "opus", "sonnet", "haiku"]
    remember({}, binary="/bin/claude")  # old CLI: no `models` at all
    remember({"models": []}, binary="/bin/claude")  # empty: keep what we have
    remember({"models": "nope"}, binary="/bin/claude")
    assert len(cached_models() or []) == 4


def test_cached_models_expire_and_are_keyed_by_binary(monkeypatch):
    remember({"models": MENU}, binary="/bin/claude")
    assert cached_models(binary="/bin/claude") is not None
    assert cached_models(binary="/other/claude") is None  # another CLI's menu
    monkeypatch.setattr(catalog, "CACHE_TTL", 0.0)
    assert cached_models(binary="/bin/claude") is None


async def test_a_process_handshake_fills_the_cache(tmp_path):
    process = ClaudeProcess(
        ProcessOptions(binary=fake_claude_bin(tmp_path, {"models": MENU}), cwd=str(tmp_path))
    )
    await process.start()
    try:
        assert [m["value"] for m in process.init_result["models"]] == [
            "default",
            "opus",
            "sonnet",
            "haiku",
        ]
    finally:
        await process.aclose()
    monkeypatch_bin = str(tmp_path / "claude")
    assert [e.id for e in cached_models(binary=monkeypatch_bin) or []] == [
        "default",
        "opus",
        "sonnet",
        "haiku",
    ]


# --- probe ------------------------------------------------------------------


async def test_list_claude_models_probes_once_and_then_serves_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", fake_claude_bin(tmp_path, {"models": MENU}))
    entries = await list_claude_models()
    assert [e.id for e in entries] == ["default", "opus", "sonnet", "haiku"]
    # The probe is handshake-only and leaves no session behind.
    argv = read_claude_argvs(tmp_path)[0]
    assert "--no-session-persistence" in argv and "--model" not in argv
    assert [m["type"] for m in read_claude_log(tmp_path)] == ["control_request"]
    assert read_claude_log(tmp_path)[0]["request"]["subtype"] == "initialize"

    again = await list_claude_models()
    assert again == entries
    assert len(read_claude_argvs(tmp_path)) == 1  # served from the cache, no relaunch


async def test_concurrent_callers_share_one_probe(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", fake_claude_bin(tmp_path, {"models": MENU}))
    a, b = await asyncio.gather(list_claude_models(), list_claude_models())
    assert a == b and [e.id for e in a] == ["default", "opus", "sonnet", "haiku"]
    assert len(read_claude_argvs(tmp_path)) == 1


async def test_missing_binary_falls_back_unless_strict(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", str(tmp_path / "missing"))
    assert await list_claude_models() == list(STATIC_MODELS)
    with pytest.raises(Exception):  # noqa: B017 - CliUnavailable
        await list_claude_models(strict=True)


async def test_old_cli_without_a_menu_falls_back_unless_strict(tmp_path, monkeypatch):
    # A handshake that answers (connected) but names no models: the picker
    # still gets the static aliases; strict callers see the empty truth.
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", fake_claude_bin(tmp_path, {}))
    assert await list_claude_models() == list(STATIC_MODELS)
    assert await list_claude_models(strict=True) == []


async def test_model_source_delegates_to_the_live_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", fake_claude_bin(tmp_path, {"models": MENU}))
    source = ModelSource(ModelConfig(provider="claude-cli", model=None, api_key=None))
    entries = await source.list_models()
    assert [e.qualified for e in entries] == [
        "claude-cli:default",
        "claude-cli:opus",
        "claude-cli:sonnet",
        "claude-cli:haiku",
    ]
