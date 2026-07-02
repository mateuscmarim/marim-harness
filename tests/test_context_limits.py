"""The ContextLimits resolver: one threshold from two concepts.

window = the model's real limit (discovered / env override); budget = the
user's cost cap (global + per-model fnmatch overrides). threshold =
min(budget, 0.8 * window) when the window is KNOWN; budget alone when it
isn't (applying a safety ratio to a made-up fallback would silently shift
the long-standing 100k default). Discovery is async and cached; the sync
threshold() never does I/O, so the status bar can call it every frame.
"""

import pytest

from marim_harness.config.context_limits import (
    DEFAULT_THRESHOLD,
    ContextLimits,
    parse_budget_overrides,
)
from marim_harness.workspace.catalog import ModelEntry


def test_parse_budget_overrides_patterns_and_unbudgeted_forms():
    parsed = parse_budget_overrides(
        "anthropic/claude-opus*=60000, openrouter/*free*=0, local/*="
    )
    assert parsed == [
        ("anthropic/claude-opus*", 60000),
        ("openrouter/*free*", None),   # 0 ⇒ unbudgeted
        ("local/*", None),             # empty ⇒ unbudgeted
    ]
    assert parse_budget_overrides("") == []
    assert parse_budget_overrides("garbage-no-equals, x=notanum") == []


def test_budget_precedence_first_match_wins_then_global():
    limits = ContextLimits(
        budget=100_000,
        budget_overrides_raw="anthropic/claude-opus*=60000,anthropic/*=90000",
    )
    assert limits.budget_for("anthropic/claude-opus-4-8") == 60_000  # first match
    assert limits.budget_for("anthropic/claude-sonnet-5") == 90_000
    assert limits.budget_for("qwen/qwen3.5-9b") == 100_000           # global


def test_override_matches_qualified_and_bare_ids():
    limits = ContextLimits(budget=None, budget_overrides_raw="qwen/*=5000")
    assert limits.budget_for("local:qwen/qwen3.5-9b") == 5000  # prefix stripped
    assert limits.budget_for("qwen/qwen3.5-9b") == 5000


def test_threshold_unknown_window_is_budget_alone():
    assert ContextLimits(budget=100_000).threshold("m") == 100_000
    assert ContextLimits(budget=None).threshold("m") == DEFAULT_THRESHOLD
    assert ContextLimits(budget=100_000).threshold(None) == 100_000


def test_threshold_known_window_applies_safety_ratio():
    limits = ContextLimits(budget=None, window_override=200_000)
    assert limits.threshold("m") == 160_000                 # 0.8 * window
    capped = ContextLimits(budget=60_000, window_override=200_000)
    assert capped.threshold("m") == 60_000                  # budget wins when lower


@pytest.mark.anyio
async def test_resolve_discovers_windows_from_catalog_once():
    calls = {"n": 0}

    async def fake_catalog():
        calls["n"] += 1
        return [ModelEntry(id="anthropic/claude-opus-4-8", name="Opus",
                           context_window=200_000)]

    limits = ContextLimits(budget=None, fetch_catalog=fake_catalog)
    assert await limits.resolve("anthropic/claude-opus-4-8") == 160_000
    assert limits.threshold("anthropic/claude-opus-4-8") == 160_000  # cached, sync
    await limits.resolve("anthropic/claude-opus-4-8")
    assert calls["n"] == 1                                   # fetched once


@pytest.mark.anyio
async def test_resolve_lmstudio_loaded_window_beats_large_budget():
    """The motivating failure: model advertises 262k, LM Studio loaded it at
    ~101k, user budget was 180k — the trigger MUST follow the loaded window."""
    async def fake_local():
        return {"qwen/qwen3.5-9b": 101_039}

    limits = ContextLimits(budget=180_000, fetch_local=fake_local)
    assert await limits.resolve("qwen/qwen3.5-9b") == int(0.8 * 101_039)


@pytest.mark.anyio
async def test_invalidate_forces_a_fresh_probe():
    windows = {"m": 8_192}
    calls = {"n": 0}

    async def fake_local():
        calls["n"] += 1
        return dict(windows)

    limits = ContextLimits(budget=None, fetch_local=fake_local)
    assert await limits.resolve("m") == int(0.8 * 8_192)
    windows["m"] = 32_768                                    # user reloads the model
    limits.invalidate()
    assert await limits.resolve("m") == int(0.8 * 32_768)
    assert calls["n"] == 2


@pytest.mark.anyio
async def test_env_window_override_beats_discovery():
    async def fake_local():
        return {"m": 500_000}

    limits = ContextLimits(budget=None, window_override=10_000,
                           fetch_local=fake_local)
    assert await limits.resolve("m") == 8_000  # 0.8 * override, discovery ignored


@pytest.mark.anyio
async def test_discovery_failure_falls_back_silently():
    async def broken():
        raise RuntimeError("boom")

    limits = ContextLimits(budget=100_000, fetch_local=broken)
    assert await limits.resolve("m") == 100_000  # budget alone; never raises
