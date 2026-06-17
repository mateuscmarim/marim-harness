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


def test_build_openrouter_model_requests_usage_accounting():
    # The built model must ask OpenRouter to include billed usage on every call.
    model = build_openrouter_model("anthropic/claude-sonnet-4-6", api_key="sk-test")
    assert model.settings is not None
    assert model.settings.get("extra_body") == {"usage": {"include": True}}


def test_build_openrouter_model_overrides_usage_mapping():
    # The model and its streamed responses must override _map_usage so the
    # captured cost is re-injected (the base class drops the float).
    model = build_openrouter_model("anthropic/claude-sonnet-4-6", api_key="sk-test")
    from pydantic_ai.models.openai import OpenAIChatModel

    assert type(model)._map_usage is not OpenAIChatModel._map_usage
