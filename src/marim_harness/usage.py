"""Token-usage breakdown and cost estimation.

pydantic-ai's :class:`RunUsage` reports ``input_tokens`` as the *inclusive*
total — the cached read/write tokens are a subset of it, not a separate bucket.
:func:`split_tokens` recovers the three buckets a human reads at a glance
(uncached in, cached, out); :func:`estimate_cost` prices a usage via the
``genai-prices`` data bundled with pydantic-ai, handling the ``provider/model``
slug OpenRouter uses for model ids.
"""

from dataclasses import dataclass
from typing import Optional

from pydantic_ai.usage import RunUsage

# Where the OpenRouter-billed cost is stashed (integer micro-USD) by the
# cost-capturing model, since RunUsage.details holds ints. See
# config/openrouter_cost.py.
COST_DETAIL_KEY = "cost_micro_usd"


@dataclass(frozen=True)
class TokenSplit:
    """A usage broken into the buckets worth showing: ``uncached_input`` is
    fresh prompt tokens, ``cache_read``/``cache_write`` are the cache hits and
    writes, and ``output`` is generated tokens. Convenience sums fold these into
    the totals a status line wants."""

    uncached_input: int
    cache_read: int
    cache_write: int
    output: int

    @property
    def cached_input(self) -> int:
        """Input tokens that went through the cache — reads plus writes."""
        return self.cache_read + self.cache_write

    @property
    def total_input(self) -> int:
        return self.uncached_input + self.cache_read + self.cache_write

    @property
    def total(self) -> int:
        return self.total_input + self.output


def split_tokens(usage: RunUsage) -> TokenSplit:
    """Break a :class:`RunUsage` into uncached-in / cache-read / cache-write /
    out. ``input_tokens`` is inclusive of the cache buckets, so the uncached
    remainder is ``input - cache_read - cache_write`` — clamped at zero so a
    provider that (wrongly) reports input disjoint from cache can't yield a
    negative bucket."""
    cache_read = usage.cache_read_tokens
    cache_write = usage.cache_write_tokens
    uncached = max(0, usage.input_tokens - cache_read - cache_write)
    return TokenSplit(uncached, cache_read, cache_write, usage.output_tokens)


def exact_cost(usage: RunUsage) -> Optional[float]:
    """The billed cost in USD if the provider reported one (captured into
    ``details[COST_DETAIL_KEY]`` as integer micro-USD), else ``None``."""
    micro = usage.details.get(COST_DETAIL_KEY)
    return micro / 1_000_000 if micro is not None else None


def resolve_cost(usage: RunUsage, model_ref: Optional[str]) -> tuple[Optional[float], bool]:
    """The best available cost as ``(usd, is_exact)``. Prefers the provider's
    billed amount (``is_exact=True``) and falls back to the genai-prices estimate
    (``is_exact=False``); ``(None, False)`` when neither is available."""
    billed = exact_cost(usage)
    if billed is not None:
        return billed, True
    return estimate_cost(usage, model_ref), False


def usage_summary(usage: RunUsage, model_ref: Optional[str]) -> dict:
    """A JSON-friendly usage breakdown: the raw input/output/total counts, the
    uncached-in / cache-read / cache-write split, and the best ``cost_usd``
    (billed when available, else estimated; ``None`` when the model isn't
    priced). ``cost_is_exact`` flags which. The canonical shape surfaced by the
    headless output and the status bar."""
    s = split_tokens(usage)
    cost, is_exact = resolve_cost(usage, model_ref)
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "uncached_input_tokens": s.uncached_input,
        "cache_read_tokens": s.cache_read,
        "cache_write_tokens": s.cache_write,
        "cost_usd": cost,
        "cost_is_exact": is_exact,
    }


def estimate_cost(usage: RunUsage, model_ref: Optional[str]) -> Optional[float]:
    """Estimate the USD cost of ``usage`` for ``model_ref`` using bundled
    ``genai-prices`` data, or ``None`` if the model isn't priced (unknown id,
    missing data). Cache reads/writes are priced at their own rates, not the
    full input rate.

    ``model_ref`` may be a bare id (``claude-sonnet-4-6``) or the
    ``provider/model`` slug OpenRouter uses (``anthropic/claude-sonnet-4-6``);
    the leading provider segment is split off into a provider hint. The price is
    the upstream provider's list price — a close estimate for OpenRouter, which
    generally bills at provider rates. Never raises."""
    if not model_ref:
        return None
    provider_id: Optional[str] = None
    ref = model_ref
    if "/" in ref:
        provider_id, ref = ref.split("/", 1)
    try:
        from genai_prices import Usage, calc_price

        priced = Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
        calc = calc_price(priced, model_ref=ref, provider_id=provider_id)
        return float(calc.total_price)
    except Exception:
        return None
