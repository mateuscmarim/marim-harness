"""Token-usage breakdown and cost estimation.

pydantic-ai's :class:`RunUsage` reports ``input_tokens`` as the *inclusive*
total — the cached read/write tokens are a subset of it, not a separate bucket.
:func:`split_tokens` recovers the three buckets a human reads at a glance
(uncached in, cached, out); :func:`estimate_cost` prices a usage via the
``genai-prices`` data bundled with pydantic-ai, handling the ``provider/model``
slug OpenRouter uses for model ids.
"""

import dataclasses
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pydantic_ai.usage import RunUsage

# Where the OpenRouter-billed cost is stashed (integer micro-USD) by the
# cost-capturing model, since RunUsage.details holds ints. See
# config/openrouter_cost.py.
COST_DETAIL_KEY = "cost_micro_usd"

# Marks usage that a subscription served rather than metered API access. API
# list prices do not apply to it, so :func:`resolve_cost` never falls back to a
# genai-prices estimate for a usage carrying this — a backend-reported billed
# amount still wins. Set by the CLI backends, which know the traffic was
# subscription-served even where the consumer only has a bare model id to go on
# (the status bar prices ``harness.model_id``, which is the raw selection).
SUBSCRIPTION_DETAIL_KEY = "subscription_tokens"

# Providers whose model name arrives bare — a name that is indistinguishable
# from the same model reached over a metered API. Qualifying the ledger's model
# column with the provider is what keeps subscription spend and API spend on
# (say) ``haiku`` from being summed into one row by ``load_models()``.
SUBSCRIPTION_SYSTEMS = frozenset({"openai-codex", "claude-cli"})


def usage_model_ref(model) -> str | None:
    """Retain subscription identity when an upstream model exposes a bare name."""
    name = getattr(model, "model_name", None)
    system = getattr(model, "system", None)
    if system in SUBSCRIPTION_SYSTEMS and name:
        return f"{system}:{name}"
    return name


def is_subscription_ref(model_ref: str | None) -> bool:
    """True when ``model_ref`` is a :data:`SUBSCRIPTION_SYSTEMS`-qualified id
    (``claude-cli:haiku``) — the form :func:`usage_model_ref` produces."""
    return bool(model_ref) and str(model_ref).split(":", 1)[0] in SUBSCRIPTION_SYSTEMS


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


def exact_cost(usage: RunUsage) -> float | None:
    """The billed cost in USD if the provider reported one (captured into
    ``details[COST_DETAIL_KEY]`` as integer micro-USD), else ``None``."""
    micro = usage.details.get(COST_DETAIL_KEY)
    return micro / 1_000_000 if micro is not None else None


def _advisor_cost(usage: RunUsage) -> tuple[float | None, bool]:
    """The banked estimate for a usage mixing the main model with an advisor
    on another provider — no single ``model_ref`` prices it, so the aggregate
    estimate recorded per delta (``preserve_usage_cost``) is the only number."""
    if usage.details.get("estimated_cost_unknown"):
        return None, False
    # The persisted detail includes pre-reload turns; RunUsage.cost only
    # includes turns since reload because the session format omits it.
    micro = usage.details.get("estimated_cost_micro_usd")
    if micro is not None:
        return micro / 1_000_000, False
    return (float(usage.cost) if usage.cost is not None else None), False


def resolve_cost(usage: RunUsage, model_ref: str | None) -> tuple[float | None, bool]:
    """The best available cost as ``(usd, is_exact)``. Prefers the provider's
    billed amount (``is_exact=True``) and falls back to the genai-prices estimate
    (``is_exact=False``); ``(None, False)`` when neither is available."""
    if usage.details.get("subscription_cost_unknown") or (
        model_ref and model_ref.startswith("openai-codex:")
    ):
        # API list prices (including upstream estimates) are not subscription
        # charges. Token accounting remains useful; money is unknown.
        return None, False
    if usage.details.get("advisor_mixed_cost"):
        return _advisor_cost(usage)
    billed = exact_cost(usage)
    if billed is not None:
        return billed, True
    if usage.details.get(SUBSCRIPTION_DETAIL_KEY) or is_subscription_ref(model_ref):
        # Subscription traffic the backend reported no amount for. Estimating it
        # would print API list prices for tokens the subscription already
        # covered — the same mistake the openai-codex guard above prevents, and
        # the one a *bare* alias only escapes by accident: "haiku" isn't in the
        # price table but "claude-haiku-4-5-20251001" (what the live catalog
        # picker offers) is. Unknown, not free.
        return None, False
    return estimate_cost(usage, model_ref), False


def preserve_usage_cost(usage: RunUsage) -> None:
    """Keep upstream's aggregate estimate in the existing integer detail bag.

    The session format predates RunUsage.cost. Recording each banked delta
    preserves its estimate without a schema change or executor-rate repricing.
    Unknown contributions remain unknown after addition and session reload.
    """
    if usage.cost is None:
        usage.details["estimated_cost_unknown"] = 1
    else:
        usage.details["estimated_cost_micro_usd"] = round(usage.cost * 1_000_000)


def usage_summary(usage: RunUsage, model_ref: str | None) -> dict:
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


def usage_from_dump(data: dict) -> RunUsage:
    """Rebuild a :class:`RunUsage` from its dict dump — the inverse of the
    ``dataclasses.asdict`` the event bus publishes (``server/host.py``'s
    ``_dump_usage``).

    An event-driven front-end receives usage as a wire dict but every consumer
    downstream (``split_tokens``, :func:`resolve_cost`, the token-split
    formatter) reads a ``RunUsage``; reconstructing one at that boundary keeps
    them byte-identical rather than teaching each of them a second shape.
    Unknown keys are dropped (a newer server may report fields this client
    doesn't model) and a dump that can't be rebuilt at all yields an empty
    usage — a mispriced card must never break the render."""
    fields = {f.name for f in dataclasses.fields(RunUsage)}
    try:
        values = {k: v for k, v in data.items() if k in fields}
        if values.get("cost") is not None:
            values["cost"] = Decimal(str(values["cost"]))
        return RunUsage(**values)
    except (TypeError, ValueError, InvalidOperation):
        return RunUsage()


def estimate_cost(usage: RunUsage, model_ref: str | None) -> float | None:
    """Estimate the USD cost of ``usage`` for ``model_ref`` using bundled
    ``genai-prices`` data, or ``None`` if the model isn't priced (unknown id,
    missing data). Cache reads/writes are priced at their own rates, not the
    full input rate.

    ``model_ref`` may be a bare id (``claude-sonnet-4-6``), a
    ``provider/model`` slug OpenRouter uses (``anthropic/claude-sonnet-4-6``),
    or a ``provider:model`` qualified form (``google:gemini-2.5-flash``);
    the leading provider segment is split off into a provider hint. The price is
    the upstream provider's list price — a close estimate for OpenRouter, which
    generally bills at provider rates. Never raises."""
    if not model_ref:
        return None
    provider_id: str | None = None
    ref = model_ref
    # Capture a leading provider: qualifier as the provider hint (model ids
    # contain no colon, per the multi-provider id scheme) so colon-qualified ids
    # like "google:gemini-2.5-flash" price via their provider — the segment is the
    # hint, not discarded. An OpenRouter-style "openrouter:anthropic/claude-..."
    # then still splits on '/' below, which overrides the hint with the real
    # upstream provider ("anthropic").
    if ":" in ref:
        provider_id, ref = ref.split(":", 1)
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
    except (ImportError, LookupError, ValueError, KeyError):
        # Expected, recoverable misses: genai-prices not installed (ImportError),
        # the model/provider not in the price table (LookupError — what calc_price
        # raises for an unknown id), or a malformed price entry. Anything else must
        # surface, not masquerade as an unpriced model — including AttributeError,
        # the classic renamed-attribute symptom of a genai-prices API change (a
        # TypeError from a changed signature is the same class of bug).
        return None
