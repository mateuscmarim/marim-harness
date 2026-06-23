from types import SimpleNamespace

from marim_harness.config.openrouter_cost import (
    MM_THINK_TAGS,
    build_openrouter_model,
    read_cost_micro_usd,
    scrub_orphan_thinking_tags,
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


def test_minimax_model_uses_native_thinking_tags():
    # MiniMax's profile must carry its own <mm:think> tags so pydantic-ai splits
    # inline reasoning into ThinkingParts instead of leaking the tags as text.
    model = build_openrouter_model("minimax/minimax-m3", api_key="sk-test")
    assert model.profile.thinking_tags == MM_THINK_TAGS


def test_non_minimax_model_keeps_default_thinking_tags():
    # The override is scoped to MiniMax; other models keep the generic profile.
    model = build_openrouter_model("anthropic/claude-sonnet-4-6", api_key="sk-test")
    assert model.profile.thinking_tags != MM_THINK_TAGS


def test_scrub_removes_orphan_closing_tag():
    # The exact leak from the screenshot: a bare closing tag with no opening.
    assert scrub_orphan_thinking_tags("the answer </mm:think> is 42", "") == (
        "the answer  is 42",
        "",
    )


def test_scrub_removes_full_inline_block():
    assert scrub_orphan_thinking_tags("a<mm:think>b</mm:think>c", "") == ("abc", "")


def test_scrub_carries_tag_split_across_chunks():
    # A tag split over two deltas must still be stripped, via the carry buffer.
    out1, carry1 = scrub_orphan_thinking_tags("done </mm:th", "")
    assert out1 == "done "
    assert carry1 == "</mm:th"
    out2, carry2 = scrub_orphan_thinking_tags("ink> next", carry1)
    assert out2 == " next"
    assert carry2 == ""


def test_scrub_leaves_ordinary_text_untouched():
    assert scrub_orphan_thinking_tags("if a < b and c > d", "") == (
        "if a < b and c > d",
        "",
    )
