"""Opt-in live smoke test: drives a full ``Harness.run_turn`` against a real
provider. Unlike the FunctionModel-mocked suite, this exercises the actual
provider contract end-to-end — request assembly, streaming, and the turn loop's
final-text extraction — so a provider/SDK drift the mocks can't see surfaces here.

Skipped unless OPENROUTER_API_KEY is exported in the real environment (it is NOT
loaded from .env), so the default suite and CI never hit the network or spend
money. Run it deliberately with:

    OPENROUTER_API_KEY=$(grep -E '^OPENROUTER_API_KEY=' .env | cut -d= -f2) \
        uv run pytest -m live -q
"""

import os

import pytest

from marim_harness.config.openrouter_cost import build_openrouter_model
from marim_harness.runtime.deps import Deps
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("OPENROUTER_API_KEY"),
        reason="OPENROUTER_API_KEY not exported; live smoke test skipped",
    ),
]


@pytest.mark.anyio
async def test_run_turn_returns_real_model_output(tmp_path):
    model = build_openrouter_model(
        "anthropic/claude-sonnet-4-6", api_key=os.environ["OPENROUTER_API_KEY"]
    )
    harness = Harness(
        model=model,
        provider=BuiltinToolProvider(),
        deps=_make_deps(tmp_path),
        instructions="You are a terse coding agent.",
    )

    out = await harness.run_turn("Reply with exactly the word: pong")

    assert "pong" in out.lower()
