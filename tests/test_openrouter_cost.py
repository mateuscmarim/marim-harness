from types import SimpleNamespace

from marim_harness.config.openrouter_cost import (
    build_openrouter_model,
    read_cost_micro_usd,
)


def _response(**usage_fields):
    return SimpleNamespace(usage=SimpleNamespace(**usage_fields))


def test_read_cost_from_typed_cost_field():
    # OpenRouter returns usage.cost as a float (dollars); we capture micro-USD.
    assert read_cost_micro_usd(_response(cost=0.000114)) == 114


def test_read_cost_from_model_extra_fallback():
    # If the SDK didn't model the field, it lands in pydantic's model_extra.
    resp = SimpleNamespace(usage=SimpleNamespace(model_extra={"cost": 0.0025}))
    assert read_cost_micro_usd(resp) == 2500


def test_read_cost_absent_is_none():
    assert read_cost_micro_usd(_response(model_extra={})) is None
    assert read_cost_micro_usd(SimpleNamespace(usage=None)) is None
    assert read_cost_micro_usd(SimpleNamespace()) is None


def test_read_cost_non_numeric_is_none():
    assert read_cost_micro_usd(_response(cost="free")) is None


def test_build_openrouter_model_enables_caching_and_usage():
    # The built model must enable OpenRouter usage accounting and place
    # cache_control breakpoints on instructions, tool defs, and messages.
    model = build_openrouter_model("anthropic/claude-sonnet-4-6", api_key="sk-test")
    s = model.settings
    assert s is not None
    assert s.get("openrouter_usage") == {"include": True}
    assert s.get("openrouter_cache_instructions") == "5m"
    assert s.get("openrouter_cache_tool_definitions") == "5m"
    assert s.get("openrouter_cache_messages") == "5m"


def test_build_openrouter_model_subclasses_openrouter_and_reinjects_cost():
    # It must subclass the official OpenRouterModel (so native cache-token
    # mapping is preserved) and override _map_usage to re-inject billed cost.
    from pydantic_ai.models.openrouter import OpenRouterModel

    model = build_openrouter_model("anthropic/claude-sonnet-4-6", api_key="sk-test")
    assert isinstance(model, OpenRouterModel)
    assert type(model)._map_usage is not OpenRouterModel._map_usage
