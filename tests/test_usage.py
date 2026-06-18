import pytest
from pydantic_ai.usage import RunUsage

from marim_harness.usage import (
    COST_DETAIL_KEY,
    TokenSplit,
    estimate_cost,
    exact_cost,
    resolve_cost,
    split_tokens,
    usage_summary,
)


def test_exact_cost_reads_billed_micro_usd_detail():
    # OpenRouter's billed cost is captured into details as integer micro-USD.
    u = RunUsage(input_tokens=13, output_tokens=5, details={COST_DETAIL_KEY: 114})
    assert exact_cost(u) == 0.000114


def test_exact_cost_absent_is_none():
    assert exact_cost(RunUsage(input_tokens=10, output_tokens=2)) is None


def test_resolve_cost_prefers_billed_over_estimate():
    # A billed detail must win over the genai-prices estimate, and be flagged
    # exact. Use an implausible billed value so it can't coincide with the model.
    u = RunUsage(
        input_tokens=56000, output_tokens=2000,
        cache_read_tokens=50000, cache_write_tokens=5000,
        details={COST_DETAIL_KEY: 999_999},
    )
    value, is_exact = resolve_cost(u, "claude-sonnet-4-6")
    assert value == 0.999999
    assert is_exact is True


def test_resolve_cost_falls_back_to_estimate():
    u = RunUsage(input_tokens=56000, output_tokens=2000)
    value, is_exact = resolve_cost(u, "claude-sonnet-4-6")
    assert value is not None and value > 0
    assert is_exact is False


def test_resolve_cost_none_when_unpriced_and_no_billed():
    value, is_exact = resolve_cost(RunUsage(input_tokens=10), "made-up-zzz")
    assert value is None
    assert is_exact is False


def test_usage_summary_marks_estimate_vs_billed():
    estimated = usage_summary(RunUsage(input_tokens=5000, output_tokens=500),
                              "claude-sonnet-4-6")
    assert estimated["cost_is_exact"] is False
    billed = usage_summary(
        RunUsage(input_tokens=5000, output_tokens=500, details={COST_DETAIL_KEY: 200}),
        "claude-sonnet-4-6",
    )
    assert billed["cost_usd"] == 0.0002
    assert billed["cost_is_exact"] is True


def test_usage_summary_carries_split_totals_and_cost():
    u = RunUsage(
        input_tokens=56000, output_tokens=2000,
        cache_read_tokens=50000, cache_write_tokens=5000,
    )
    d = usage_summary(u, "claude-sonnet-4-6")
    assert d["input_tokens"] == 56000
    assert d["output_tokens"] == 2000
    assert d["total_tokens"] == 58000
    assert d["uncached_input_tokens"] == 1000
    assert d["cache_read_tokens"] == 50000
    assert d["cache_write_tokens"] == 5000
    assert d["cost_usd"] is not None and d["cost_usd"] > 0


def test_usage_summary_cost_is_none_for_unknown_model():
    u = RunUsage(input_tokens=1000, output_tokens=100)
    d = usage_summary(u, "made-up-model-zzz")
    assert d["cost_usd"] is None
    # The breakdown is still present even when pricing is unavailable.
    assert d["uncached_input_tokens"] == 1000


def test_split_separates_uncached_input_cache_and_output():
    # input_tokens is the inclusive total (cached + uncached), per pydantic-ai's
    # normalization — so the uncached remainder is input minus the cache buckets.
    u = RunUsage(
        input_tokens=56000, output_tokens=2000,
        cache_read_tokens=50000, cache_write_tokens=5000,
    )
    s = split_tokens(u)
    assert isinstance(s, TokenSplit)
    assert s.uncached_input == 1000  # 56000 - 50000 - 5000
    assert s.cache_read == 50000
    assert s.cache_write == 5000
    assert s.output == 2000
    assert s.cached_input == 55000  # read + write
    assert s.total_input == 56000
    assert s.total == 58000


def test_split_with_no_cache_is_just_in_and_out():
    u = RunUsage(input_tokens=1000, output_tokens=500)
    s = split_tokens(u)
    assert s.uncached_input == 1000
    assert s.cached_input == 0
    assert s.output == 500
    assert s.total == 1500


def test_split_clamps_when_provider_reports_input_excluding_cache():
    # Defensive: a provider that reports input_tokens disjoint from the cache
    # buckets would make (input - cache) negative; uncached must never go < 0.
    u = RunUsage(input_tokens=1000, output_tokens=200, cache_read_tokens=50000)
    s = split_tokens(u)
    assert s.uncached_input == 0


def test_estimate_cost_known_model_is_positive():
    u = RunUsage(
        input_tokens=56000, output_tokens=2000,
        cache_read_tokens=50000, cache_write_tokens=5000,
    )
    cost = estimate_cost(u, "claude-sonnet-4-6")
    assert cost is not None and cost > 0


def test_estimate_cost_strips_openrouter_provider_prefix():
    u = RunUsage(
        input_tokens=56000, output_tokens=2000,
        cache_read_tokens=50000, cache_write_tokens=5000,
    )
    bare = estimate_cost(u, "claude-sonnet-4-6")
    prefixed = estimate_cost(u, "anthropic/claude-sonnet-4-6")
    # The "anthropic/" routing prefix OpenRouter uses must be handled, yielding
    # the same price as the bare model id.
    assert prefixed is not None
    assert prefixed == bare


def test_estimate_cost_reflects_cache_discount():
    # The same total token count costs less when most of the input is cached
    # reads (priced ~0.1x), proving cache tokens are costed separately.
    cached = RunUsage(input_tokens=56000, output_tokens=2000,
                      cache_read_tokens=55000)
    uncached = RunUsage(input_tokens=56000, output_tokens=2000)
    assert estimate_cost(cached, "claude-sonnet-4-6") < estimate_cost(
        uncached, "claude-sonnet-4-6"
    )


def test_estimate_cost_unknown_model_is_none():
    u = RunUsage(input_tokens=1000, output_tokens=100)
    assert estimate_cost(u, "totally-made-up-model-zzz") is None


def test_estimate_cost_no_model_is_none():
    u = RunUsage(input_tokens=1000, output_tokens=100)
    assert estimate_cost(u, None) is None
    assert estimate_cost(u, "") is None


def test_estimate_cost_propagates_programming_errors(monkeypatch):
    """A genuine bug (e.g. a genai-prices API change) must surface, not be
    swallowed into a silent None alongside the expected 'unknown model' case —
    otherwise a broken cost path looks identical to an unpriced model."""
    import genai_prices

    def boom(*a, **k):
        raise TypeError("calc_price() got an unexpected keyword argument")

    monkeypatch.setattr(genai_prices, "calc_price", boom)
    u = RunUsage(input_tokens=1000, output_tokens=100)
    with pytest.raises(TypeError):
        estimate_cost(u, "claude-sonnet-4-6")
