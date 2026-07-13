import asyncio

from marim_harness.config.model import SubagentTiers
from marim_harness.subagents.runner import _resolve_spawn_model_id
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


def test_resolve_spawn_model_id_read_only_uses_cheap_tier():
    tiers = SubagentTiers(cheap="p:c", high="p:h")
    got = _resolve_spawn_model_id(
        override_tier=None, slug=None, spec_tier=None, read_only=True, tiers=tiers
    )
    assert got == "p:c"


def test_resolve_spawn_model_id_mutating_uses_high_tier():
    tiers = SubagentTiers(cheap="p:c", high="p:h")
    got = _resolve_spawn_model_id(
        override_tier=None, slug=None, spec_tier=None, read_only=False, tiers=tiers
    )
    assert got == "p:h"


def test_resolve_spawn_model_id_override_tier_wins():
    tiers = SubagentTiers(cheap="p:c", high="p:h")
    got = _resolve_spawn_model_id(
        override_tier="cheap", slug=None, spec_tier=None, read_only=False, tiers=tiers
    )
    assert got == "p:c"


def test_resolve_spawn_model_id_unset_tier_inherits_main():
    # med is unconfigured → None means "inherit the main model".
    tiers = SubagentTiers(cheap="p:c")
    got = _resolve_spawn_model_id(
        override_tier="med", slug=None, spec_tier=None, read_only=True, tiers=tiers
    )
    assert got is None


def test_resolve_spawn_model_id_slug_in_allowlist_honored():
    tiers = SubagentTiers(cheap="p:c", high="p:h")
    got = _resolve_spawn_model_id(
        override_tier=None, slug="p:h", spec_tier=None, read_only=True, tiers=tiers
    )
    assert got == "p:h"


def test_resolve_spawn_model_id_slug_out_of_allowlist_falls_back():
    tiers = SubagentTiers(cheap="p:c", high="p:h")
    got = _resolve_spawn_model_id(
        override_tier=None, slug="p:evil", spec_tier=None, read_only=True, tiers=tiers
    )
    assert got == "p:c"  # dropped to the read-only default tier


def test_resolve_spawn_model_id_no_tiers_configured_passes_slug_through():
    # Legacy behavior: with no tiers set, any slug override is honored as-is.
    got = _resolve_spawn_model_id(
        override_tier=None, slug="p:anything", spec_tier=None, read_only=True,
        tiers=SubagentTiers(),
    )
    assert got == "p:anything"


def test_resolve_spawn_model_id_no_tiers_read_only_inherits_main():
    got = _resolve_spawn_model_id(
        override_tier=None, slug=None, spec_tier=None, read_only=True,
        tiers=SubagentTiers(),
    )
    assert got is None


def test_disabled_tiers_model_for_is_none_despite_configured_slugs():
    # The disable switch bypasses routing WITHOUT clearing the curated slugs:
    # every tier reports None (⇒ inherit the main model) while cheap/med/high
    # stay populated for when tiering is turned back on.
    tiers = SubagentTiers(cheap="p:c", med="p:m", high="p:h", enabled=False)
    assert tiers.model_for("cheap") is None
    assert tiers.model_for("med") is None
    assert tiers.model_for("high") is None
    # The curated slugs are untouched — re-enabling restores routing verbatim.
    assert (tiers.cheap, tiers.med, tiers.high) == ("p:c", "p:m", "p:h")


def test_disabled_tiers_allowlist_is_empty():
    tiers = SubagentTiers(cheap="p:c", high="p:h", enabled=False)
    assert tiers.allowlist() == frozenset()


def test_disabled_tiers_default_is_enabled():
    # Backwards compatible: an unconfigured install still routes by tier.
    assert SubagentTiers().enabled is True


def test_resolve_spawn_model_id_disabled_tiers_inherit_main():
    # With tiering disabled, a mutating spawn that would route to the high tier
    # instead inherits the main model (None).
    tiers = SubagentTiers(cheap="p:c", high="p:h", enabled=False)
    got = _resolve_spawn_model_id(
        override_tier=None, slug=None, spec_tier=None, read_only=False, tiers=tiers
    )
    assert got is None


def test_resolve_spawn_model_id_disabled_tiers_still_honor_explicit_slug():
    # Disabling tiering is not a slug lockout: an empty allowlist reverts to the
    # legacy passthrough, so an explicit model= override is still honored.
    tiers = SubagentTiers(cheap="p:c", high="p:h", enabled=False)
    got = _resolve_spawn_model_id(
        override_tier=None, slug="p:explicit", spec_tier=None, read_only=True, tiers=tiers
    )
    assert got == "p:explicit"


def test_run_forwards_tier_to_execute_spawn(monkeypatch):
    from marim_harness.subagents.runner import SubagentRunner

    captured = {}

    async def fake_exec(self, *a, **kw):
        captured.update(kw)
        return "ok"

    monkeypatch.setattr(SubagentRunner, "_execute_spawn", fake_exec)
    runner = object.__new__(SubagentRunner)  # bypass __init__; only run() is exercised
    out = asyncio.run(
        SubagentRunner.run(
            runner, "explore", "task", "sid", None, None, None, None, 0, "cheap", None
        )
    )
    assert out == "ok"
    assert captured.get("tier") == "cheap"


def test_run_background_forwards_tier_to_execute_spawn(monkeypatch):
    from marim_harness.subagents.runner import SubagentRunner

    captured = {}

    async def fake_exec(self, *a, **kw):
        captured.update(kw)
        return "ok"

    monkeypatch.setattr(SubagentRunner, "_execute_spawn", fake_exec)
    runner = object.__new__(SubagentRunner)  # bypass __init__; only run_background() is exercised
    out = asyncio.run(
        SubagentRunner.run_background(
            runner, "explore", "task", None, None, None, None, "sid", 0, "high"
        )
    )
    assert out == "ok"
    assert captured.get("tier") == "high"
