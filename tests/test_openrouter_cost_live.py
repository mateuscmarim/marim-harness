"""Opt-in live test: proves the cost-capture model records OpenRouter's billed
cost end-to-end. Skipped unless OPENROUTER_API_KEY is exported in the real
environment (it is NOT loaded from .env), so the default suite never hits the
network or spends money. Run it deliberately with:

    OPENROUTER_API_KEY=$(grep -E '^OPENROUTER_API_KEY=' .env | cut -d= -f2) \
        uv run pytest tests/test_openrouter_cost_live.py -q
"""

import os

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("OPENROUTER_API_KEY"),
        reason="OPENROUTER_API_KEY not exported; live OpenRouter test skipped",
    ),
]


@pytest.mark.anyio
async def test_streamed_turn_captures_billed_cost():
    from pydantic_ai import Agent

    from marim_harness.config.openrouter_cost import build_openrouter_model
    from marim_harness.usage import estimate_cost, exact_cost, resolve_cost

    model = build_openrouter_model(
        "anthropic/claude-sonnet-4-6", api_key=os.environ["OPENROUTER_API_KEY"]
    )
    agent = Agent(model)
    async with agent.run_stream("Reply with exactly: pong") as run:
        await run.get_output()
        usage = run.usage

    billed = exact_cost(usage)
    assert billed is not None and billed > 0, usage.details

    value, is_exact = resolve_cost(usage, "anthropic/claude-sonnet-4-6")
    assert is_exact is True
    assert value == billed

    # The genai-prices estimate should be in the same ballpark as the billed
    # amount (OpenRouter bills at provider rates), validating the fallback.
    est = estimate_cost(usage, "anthropic/claude-sonnet-4-6")
    if est:
        assert abs(billed - est) / billed < 0.5
