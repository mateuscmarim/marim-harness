"""The ContextLimits resolver: one threshold from two concepts.

window = the model's real limit (discovered / env override); budget = the
user's cost cap (global + per-model fnmatch overrides). threshold =
min(budget, 0.8 * window) when the window is KNOWN; budget alone when it
isn't (applying a safety ratio to a made-up fallback would silently shift
the long-standing 100k default). Discovery is async and cached; the sync
threshold() never does I/O, so the status bar can call it every frame.
"""

import asyncio
from types import SimpleNamespace

import pytest

from marim_harness.config.context_limits import (
    _PROVIDER_PREFIXES,
    DEFAULT_THRESHOLD,
    ContextLimits,
    _bare_id,
    build_context_limits,
    parse_budget_overrides,
)
from marim_harness.config.model import KNOWN_PROVIDERS
from marim_harness.workspace.catalog import ModelEntry


def test_provider_prefixes_mirrors_known_providers():
    """_PROVIDER_PREFIXES is a hand-maintained mirror of KNOWN_PROVIDERS (not
    imported: config/model.py pulls in catalog/notification machinery and this
    module must stay light). A provider added to one and not the other means
    _bare_id() silently fails to strip its qualifier — this test is the
    tripwire for the next provider."""
    assert _PROVIDER_PREFIXES == KNOWN_PROVIDERS


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


def test_bare_id_survives_ollama_style_tags():
    """Model ids CAN contain colons (Ollama tags like ``qwen2.5-coder:7b``) —
    only a known provider name before the first colon is a qualifier."""
    assert _bare_id("qwen2.5-coder:7b") == "qwen2.5-coder:7b"      # tag, not provider
    assert _bare_id("local:qwen/qwen3.5-9b") == "qwen/qwen3.5-9b"  # real qualifier
    assert _bare_id("local:qwen2.5-coder:7b") == "qwen2.5-coder:7b"


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


# -- multi-source discovery ------------------------------------------------


@pytest.mark.anyio
async def test_resolve_merges_windows_from_all_fetchers():
    """Several active providers ⇒ several discovery sources; a /model switch may
    land on any of them, so resolve() must merge every source's windows."""
    async def source_a():
        return {"m1": 100_000}

    async def source_b():
        return {"m2": 50_000}

    limits = ContextLimits(budget=None, fetchers=[source_a, source_b])
    assert await limits.resolve("m1") == 80_000
    assert limits.threshold("m2") == 40_000  # the other source landed too


@pytest.mark.anyio
async def test_per_source_failure_does_not_poison_the_others():
    async def broken():
        raise RuntimeError("catalog down")

    async def good():
        return {"m": 10_000}

    limits = ContextLimits(budget=None, fetchers=[broken, good])
    assert await limits.resolve("m") == 8_000  # good source survives


@pytest.mark.anyio
async def test_build_context_limits_probes_all_active_providers(monkeypatch):
    """The motivating recurrence: /model accepts qualified ids like
    ``local:qwen/...``, so a switch can land on a NON-default provider — its
    window must still be discoverable (else the threshold silently falls back
    to the budget and requests overflow again)."""
    from marim_harness.workspace import catalog

    async def fake_openrouter(api_key=None, timeout=10.0):
        return [ModelEntry(id="anthropic/claude-opus-4-8", name="Opus",
                           context_window=200_000)]

    async def fake_lmstudio(base_url, api_key=None, timeout=10.0):
        return {"qwen/qwen3.5-9b": 101_039}

    monkeypatch.setattr(catalog, "fetch_openrouter_models", fake_openrouter)
    monkeypatch.setattr(catalog, "fetch_lmstudio_windows", fake_lmstudio)
    configs = {
        "openrouter": SimpleNamespace(base_url=None, api_key="sk-x"),
        "local": SimpleNamespace(base_url="http://localhost:1234/v1", api_key="lmstudio"),
    }
    limits = build_context_limits(configs, window_override=None, budget=None)
    assert await limits.resolve("anthropic/claude-opus-4-8") == 160_000
    # Simulate a /model switch to the OTHER provider's model (invalidate +
    # resolve): the local fetcher, one source among several, must be consulted.
    limits.invalidate()
    assert await limits.resolve("local:qwen/qwen3.5-9b") == int(0.8 * 101_039)


# -- single-flight resolve + generation-guarded invalidate ------------------


@pytest.mark.anyio
async def test_concurrent_resolves_share_one_fetch_and_both_see_the_window():
    """A parallel spawn fan-out calls resolve() concurrently; each spawn freezes
    the returned threshold into its masker. The second caller must await the
    SAME in-flight fetch — not observe a budget-only threshold computed from
    still-empty windows."""
    gate = asyncio.Event()
    calls = {"n": 0}

    async def gated():
        calls["n"] += 1
        await gate.wait()
        return {"m": 10_000}

    limits = ContextLimits(budget=100_000, fetchers=[gated])
    first = asyncio.create_task(limits.resolve("m"))
    second = asyncio.create_task(limits.resolve("m"))
    await asyncio.sleep(0)  # both callers reach the in-flight fetch
    gate.set()
    assert await first == 8_000
    assert await second == 8_000  # NOT 100_000 from empty windows
    assert calls["n"] == 1        # single flight


@pytest.mark.anyio
async def test_invalidate_mid_fetch_discards_the_stale_result():
    """invalidate() during an in-flight fetch (a /model switch mid-turn) must
    not be undone when the old coroutine's results land: the late result is
    discarded and the next resolve() re-fetches fresh values."""
    gate = asyncio.Event()
    calls = {"n": 0}
    results = [{"m": 999_999}, {"m": 10_000}]  # stale then fresh

    async def gated():
        calls["n"] += 1
        result = results[calls["n"] - 1]
        if calls["n"] == 1:
            await gate.wait()  # park the FIRST fetch so invalidate() overlaps
        return result

    limits = ContextLimits(budget=50_000, fetchers=[gated])
    stale = asyncio.create_task(limits.resolve("m"))
    await asyncio.sleep(0)      # the first fetch is parked on the gate
    limits.invalidate()         # model switched while the fetch is in flight
    gate.set()
    await stale                 # old resolve completes without committing
    assert limits.threshold("m") == 50_000       # stale window NOT resurrected
    assert await limits.resolve("m") == 8_000    # fresh re-fetch after invalidate
    assert calls["n"] == 2


def test_window_for_returns_the_override():
    limits = ContextLimits(window_override=102_206)
    assert limits.window_for("any-model") == 102_206
    assert limits.window_for(None) == 102_206


@pytest.mark.anyio
async def test_window_for_returns_the_discovered_window_or_none():
    """The raw KNOWN window (no safety ratio, no budget) — the number the
    contention classifier compares a rejected request's size against."""
    async def fake_local():
        return {"ornith-1.0-9b": 102_206}

    limits = ContextLimits(budget=100_000, fetch_local=fake_local)
    assert limits.window_for("ornith-1.0-9b") is None       # not discovered yet
    await limits.resolve("ornith-1.0-9b")
    assert limits.window_for("ornith-1.0-9b") == 102_206    # raw, not 0.8x
    assert limits.window_for("local:ornith-1.0-9b") == 102_206  # qualified id
    assert limits.window_for("unknown-model") is None
