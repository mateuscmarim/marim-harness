"""The thinking-level core: the ordered vocabulary and the three pure helpers
(parse/settings_for/resolve). No live models, no ModelSettings side effects —
settings_for must never mutate its base."""

from pydantic_ai.settings import ModelSettings

from marim_harness.thinking import (
    THINKING_LEVELS,
    parse_thinking_level,
    resolve_thinking,
    settings_for,
)


def test_levels_are_the_frozen_ordered_vocabulary():
    # Persisted into session JSON and read by the UI/config; this order and
    # spelling is the single source of truth. off is FIRST (the disable).
    assert THINKING_LEVELS == ("off", "minimal", "low", "medium", "high", "xhigh")


def test_parse_accepts_every_level_case_insensitively():
    for level in THINKING_LEVELS:
        assert parse_thinking_level(level) == level
        assert parse_thinking_level(level.upper()) == level
    assert parse_thinking_level("  High  ") == "high"


def test_parse_rejects_unknown_and_blank_and_none():
    assert parse_thinking_level(None) is None
    assert parse_thinking_level("") is None
    assert parse_thinking_level("   ") is None
    assert parse_thinking_level("ultra") is None
    assert parse_thinking_level("true") is None


def test_settings_for_off_and_none_return_the_base_unchanged():
    base = ModelSettings(parallel_tool_calls=True)
    assert settings_for("off", base) == base
    assert settings_for(None, base) == base
    # off must OMIT the key — never thinking=False (backward-compatible).
    assert "thinking" not in settings_for("off", base)


def test_settings_for_a_level_merges_without_mutating_base():
    base = ModelSettings(parallel_tool_calls=True)
    out = settings_for("high", base)
    assert out["thinking"] == "high"
    assert out["parallel_tool_calls"] is True
    # base is not mutated: settings_for returns a NEW mapping.
    assert "thinking" not in base


def test_resolve_precedence_override_then_spec_then_inherited():
    assert resolve_thinking("high", "low", "medium") == "high"
    assert resolve_thinking(None, "low", "medium") == "low"
    assert resolve_thinking(None, None, "medium") == "medium"
    assert resolve_thinking(None, None, None) is None


def test_resolve_unrecognized_candidate_falls_through():
    # A raw model slug fat-fingered into the thinking slot, or a typo'd label,
    # degrades to the next level rather than erroring (mirrors resolve_tier).
    assert resolve_thinking("openrouter:opus", "medium", None) == "medium"
    assert resolve_thinking("bogus", "also-bogus", "low") == "low"
    assert resolve_thinking("bogus", None, None) is None


def test_resolve_off_is_an_explicit_choice_that_wins():
    # off is a real member of the vocabulary: an explicit off beats an
    # inherited level (settings_for then omits the key).
    assert resolve_thinking("off", "high", "high") == "off"
    assert resolve_thinking(None, "off", "high") == "off"
