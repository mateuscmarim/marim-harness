from pydantic_ai.usage import RunUsage

from marim_harness.usage import (
    TokenSplit,
    estimate_cost,
    split_tokens,
    usage_summary,
)


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
