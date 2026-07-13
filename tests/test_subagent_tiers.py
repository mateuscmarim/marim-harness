from marim_harness.config.model import SubagentTiers
from marim_harness.subagents.tiers import TIER_NAMES, resolve_tier


def test_override_wins_when_valid():
    assert resolve_tier("cheap", "high", read_only=False) == "cheap"


def test_override_ignored_when_not_a_tier_name():
    # A raw slug passed where a tier name is expected falls through to the spec label.
    assert resolve_tier("anthropic/claude-opus", "med", read_only=True) == "med"


def test_spec_tier_used_when_no_override():
    assert resolve_tier(None, "med", read_only=True) == "med"


def test_spec_tier_ignored_when_not_a_tier_name():
    assert resolve_tier(None, "bogus", read_only=True) == "cheap"


def test_tool_reach_default_read_only_is_cheap():
    assert resolve_tier(None, None, read_only=True) == "cheap"


def test_tool_reach_default_mutating_is_high():
    assert resolve_tier(None, None, read_only=False) == "high"


def test_tier_names_are_exact():
    assert TIER_NAMES == ("cheap", "med", "high")


def test_model_for_maps_names():
    tiers = SubagentTiers(cheap="p:c", med="p:m", high="p:h")
    assert tiers.model_for("cheap") == "p:c"
    assert tiers.model_for("med") == "p:m"
    assert tiers.model_for("high") == "p:h"


def test_model_for_unset_tier_is_none():
    assert SubagentTiers().model_for("cheap") is None


def test_model_for_unknown_name_is_none():
    assert SubagentTiers(cheap="p:c").model_for("bogus") is None


def test_allowlist_drops_unset():
    tiers = SubagentTiers(cheap="p:c", high="p:h")
    assert tiers.allowlist() == frozenset({"p:c", "p:h"})
