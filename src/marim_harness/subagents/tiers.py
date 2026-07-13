"""Pure sub-agent model-tier routing — no marim imports, unit-tested directly.

A spawn's tier is resolved from three inputs, highest precedence first: an
explicit override tier named by the spawning model, the sub-agent spec's own
``tier:`` label, then a tool-reach default (read-only fan-out is cheap,
workspace-mutating work is high). ``med`` is deliberately the opt-in middle:
reachable only via an override or a spec label, never from tool reach alone.

An override or spec value that is not one of the three tier names falls through
to the next level rather than erroring — a raw model slug passed through the
override slot, or a typo'd label, degrades to the automatic default instead of
breaking the spawn. The caller logs when it drops an out-of-range value."""

TIER_NAMES: tuple[str, ...] = ("cheap", "med", "high")


def resolve_tier(override: str | None, spec_tier: str | None, read_only: bool) -> str:
    """Return the tier name for a spawn: ``override`` if it is a tier name, else
    ``spec_tier`` if it is a tier name, else the tool-reach default (``cheap``
    for read-only, ``high`` for mutating). Always returns a member of
    ``TIER_NAMES``."""
    for candidate in (override, spec_tier):
        if candidate in TIER_NAMES:
            return candidate  # type: ignore[return-value]  # membership-checked above
    return "cheap" if read_only else "high"
