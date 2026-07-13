# Sub-agent Model Tiers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user curate three model tiers (cheap/med/high) for sub-agents; each spawn resolves to a tier automatically (spec label or tool reach) with the main model able to override per-spawn by tier name.

**Architecture:** A pure resolver maps `(override, spec-tier, read-only) → tier name`; a `SubagentTiers` config maps tier name → model id (empty → inherit main). `SubagentRunner.build` runs the resolver and builds the resulting id through the existing `_build_model` closure. The main model overrides by tier name via a new `spawn_agent(tier=...)` param threaded to `build`; the existing `model=` slug stays as a bounded escape hatch.

**Tech Stack:** Python 3.10+, Pydantic AI, Textual, pytest, uv.

## Global Constraints

- `requires-python >=3.10` — no 3.11+ only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity ≤ 10.
- Use `uv run …` for all commands; never bare `python`/`pytest`/`pip`.
- CI order is ruff → pyright → pytest; match it locally before claiming done.
- Pure decision/parse helpers stay side-effect-free and unit-tested directly.
- Tool docstrings are model-facing product copy — write them as such.
- Tier names are exactly `cheap`, `med`, `high` (verbatim, lowercase).
- Env var names are exactly `MARIM_SUBAGENT_TIER_CHEAP`, `MARIM_SUBAGENT_TIER_MED`, `MARIM_SUBAGENT_TIER_HIGH`; each value is a qualified `provider:model_id` string.
- Safe-by-default: with no tiers configured, behavior is identical to today (unset tier → inherit main; the `model=` slug escape hatch keeps its legacy passthrough).
- Mutating spawns default to **high**; the main-model override is **by tier name** (slug is the escape hatch). [Locked defaults from the spec.]

---

## File Structure

- **Create** `src/marim_harness/subagents/tiers.py` — pure: `TIER_NAMES`, `resolve_tier`. No marim imports (leaf).
- **Modify** `src/marim_harness/config/model.py` — add `SubagentTiers` dataclass, add `tiers` field to `SubagentConfig`, parse the three env vars in `_common_config`.
- **Modify** `src/marim_harness/workspace/agents.py` — add `AgentDef.tier`, parse `tier:` frontmatter.
- **Modify** `src/marim_harness/subagents/runner.py` — `SubagentRunner.__init__` takes `tiers`; `build` runs the resolver; thread `tier` through `run` / `run_background` / `_execute_spawn` / `_prepare_spawn`.
- **Modify** `src/marim_harness/runtime/deps.py` — add the tier slot to `SubAgentRunner` and `BackgroundAgentRunner` Callable aliases.
- **Modify** `src/marim_harness/tools/spawn_tools.py` — add `tier` param to `spawn_agent` + `_spawn_background`; thread to the service calls; rewrite the `model`/`tier` docstring block.
- **Modify** `src/marim_harness/runtime/harness.py` — `HarnessConfig.subagent_tiers` field; pass `tiers=` into `SubagentRunner`.
- **Modify** `src/marim_harness/runtime/bootstrap.py` — pass `subagent_tiers=cfg.subagent.tiers` into `with_config_overrides`.
- **Modify** `src/marim_harness/interfaces/tui/settings.py` — three tier pickers writing the env + refreshing live.
- **Tests:** `tests/test_subagent_tiers.py` (new), plus additions to `tests/test_config.py`, `tests/test_agents.py` (the existing `_parse_agent` test module), `tests/test_settings_screen.py`, `tests/test_bootstrap.py`.

---

## Task 1: Pure tier config + resolver

**Files:**
- Create: `src/marim_harness/subagents/tiers.py`
- Modify: `src/marim_harness/config/model.py` (add `SubagentTiers`, extend `SubagentConfig`, parse env in `_common_config`)
- Test: `tests/test_subagent_tiers.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `TIER_NAMES: tuple[str, ...] = ("cheap", "med", "high")` in `subagents/tiers.py`
  - `resolve_tier(override: str | None, spec_tier: str | None, read_only: bool) -> str` in `subagents/tiers.py` — returns one of `TIER_NAMES`.
  - `SubagentTiers` dataclass in `config/model.py` with fields `cheap/med/high: str | None = None`, methods `model_for(self, tier: str) -> str | None` and `allowlist(self) -> frozenset[str]`.
  - `SubagentConfig.tiers: SubagentTiers` (default `SubagentTiers()`).

- [ ] **Step 1: Write the failing resolver test**

Create `tests/test_subagent_tiers.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_subagent_tiers.py -v`
Expected: FAIL — `ModuleNotFoundError: marim_harness.subagents.tiers`.

- [ ] **Step 3: Implement the pure resolver**

Create `src/marim_harness/subagents/tiers.py`:

```python
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
```

- [ ] **Step 4: Run the resolver test to verify it passes**

Run: `uv run pytest tests/test_subagent_tiers.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Write the failing SubagentTiers test**

Append to `tests/test_subagent_tiers.py`:

```python
from marim_harness.config.model import SubagentTiers


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
```

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/test_subagent_tiers.py -k SubagentTiers -v`
Expected: FAIL — `ImportError: cannot import name 'SubagentTiers'`.

- [ ] **Step 7: Implement `SubagentTiers` and extend `SubagentConfig`**

In `src/marim_harness/config/model.py`, add the dataclass immediately above `class SubagentConfig` (around line 69):

```python
@dataclass(frozen=True)
class SubagentTiers:
    """The user-curated model per sub-agent tier. Each value is a qualified
    ``provider:model_id`` (or None ⇒ inherit the main model). The set of
    non-None ids is the allowlist a raw ``model=`` slug override is bounded to
    once any tier is configured."""

    cheap: str | None = None
    med: str | None = None
    high: str | None = None

    def model_for(self, tier: str) -> str | None:
        """The configured model id for ``tier``, or None (unset ⇒ inherit main)."""
        return {"cheap": self.cheap, "med": self.med, "high": self.high}.get(tier)

    def allowlist(self) -> frozenset[str]:
        """The non-None tier model ids — the permitted set for a slug override."""
        return frozenset(m for m in (self.cheap, self.med, self.high) if m)
```

Then add the field to `SubagentConfig` (after `request_limit`, around line 81):

```python
    # The user-curated model per sub-agent tier (cheap/med/high). Empty tiers
    # inherit the main model, so an unconfigured install behaves like today.
    tiers: "SubagentTiers" = field(default_factory=SubagentTiers)
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_tiers.py -v`
Expected: PASS (11 passed).

- [ ] **Step 9: Write the failing env-parse test**

Add to `tests/test_config.py` (imports at top already cover `_common_config`/`load_config`; use monkeypatch on env):

```python
def test_subagent_tiers_parsed_from_env(monkeypatch):
    from marim_harness.config.model import load_config
    monkeypatch.setenv("MARIM_SUBAGENT_TIER_CHEAP", "local:ornith-1.0-9b")
    monkeypatch.setenv("MARIM_SUBAGENT_TIER_HIGH", "openrouter:anthropic/claude-opus-4")
    cfg = load_config()
    assert cfg.subagent.tiers.cheap == "local:ornith-1.0-9b"
    assert cfg.subagent.tiers.med is None
    assert cfg.subagent.tiers.high == "openrouter:anthropic/claude-opus-4"


def test_subagent_tiers_default_empty(monkeypatch):
    from marim_harness.config.model import load_config
    monkeypatch.delenv("MARIM_SUBAGENT_TIER_CHEAP", raising=False)
    monkeypatch.delenv("MARIM_SUBAGENT_TIER_MED", raising=False)
    monkeypatch.delenv("MARIM_SUBAGENT_TIER_HIGH", raising=False)
    cfg = load_config()
    assert cfg.subagent.tiers.allowlist() == frozenset()
```

- [ ] **Step 10: Run it to verify it fails**

Run: `uv run pytest tests/test_config.py -k subagent_tiers -v`
Expected: FAIL — `tiers.cheap` is None (env not read yet).

- [ ] **Step 11: Parse the env vars in `_common_config`**

In `src/marim_harness/config/model.py`, inside `_common_config` where `subagent = SubagentConfig(...)` is built (around line 217), add a `tiers=` argument. Use the existing `_opt_str`-style pattern with `os.getenv`; a blank/absent value stays None:

```python
    subagent = SubagentConfig(
        concurrency=_parse_concurrency(
            os.getenv("MARIM_SUBAGENT_CONCURRENCY"), DEFAULT_SUBAGENT_CONCURRENCY
        ),
        transcript_cap=_int_env("MARIM_SUBAGENT_TRANSCRIPT_CAP", 2000),
        request_limit=_int_env("MARIM_SUBAGENT_REQUEST_LIMIT", 50),
        tiers=SubagentTiers(
            cheap=(os.getenv("MARIM_SUBAGENT_TIER_CHEAP") or None),
            med=(os.getenv("MARIM_SUBAGENT_TIER_MED") or None),
            high=(os.getenv("MARIM_SUBAGENT_TIER_HIGH") or None),
        ),
    )
```

(Keep any other existing `SubagentConfig(...)` kwargs already present — only the `tiers=` line is new.)

- [ ] **Step 12: Run the config test to verify it passes**

Run: `uv run pytest tests/test_config.py -k subagent_tiers -v`
Expected: PASS (2 passed).

- [ ] **Step 13: Commit**

```bash
git add src/marim_harness/subagents/tiers.py src/marim_harness/config/model.py tests/test_subagent_tiers.py tests/test_config.py
git commit -m "feat(subagents): pure tier resolver + SubagentTiers config"
```

---

## Task 2: `tier:` frontmatter on sub-agent specs

**Files:**
- Modify: `src/marim_harness/workspace/agents.py` (`AgentDef.tier`, parse in `_parse_agent`)
- Test: `tests/test_agents.py` (the module that already tests `_parse_agent`/`discover_agents`)

**Interfaces:**
- Consumes: `TIER_NAMES` from `subagents.tiers` (Task 1).
- Produces: `AgentDef.tier: str | None` — the spec's declared tier, or None when absent/invalid.

- [ ] **Step 1: Write the failing parse test**

Add to `tests/test_agents.py` (the module that already calls `_parse_agent`). Example:

```python
def test_parse_agent_reads_valid_tier(tmp_path):
    from marim_harness.workspace.agents import _parse_agent
    p = tmp_path / "researcher.md"
    p.write_text(
        "---\ndescription: deep read\ntier: med\n---\nDo research.\n",
        encoding="utf-8",
    )
    defn = _parse_agent("project", p)
    assert defn is not None
    assert defn.tier == "med"


def test_parse_agent_drops_invalid_tier(tmp_path):
    from marim_harness.workspace.agents import _parse_agent
    p = tmp_path / "bad.md"
    p.write_text(
        "---\ndescription: x\ntier: enormous\n---\nBody.\n", encoding="utf-8"
    )
    defn = _parse_agent("project", p)
    assert defn is not None
    assert defn.tier is None


def test_parse_agent_tier_absent_is_none(tmp_path):
    from marim_harness.workspace.agents import _parse_agent
    p = tmp_path / "plain.md"
    p.write_text("---\ndescription: x\n---\nBody.\n", encoding="utf-8")
    defn = _parse_agent("project", p)
    assert defn is not None
    assert defn.tier is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_agents.py -k tier -v`
Expected: FAIL — `AttributeError: 'AgentDef' object has no attribute 'tier'`.

- [ ] **Step 3: Add the field and import**

In `src/marim_harness/workspace/agents.py`, extend the existing import from `..tools.names` line (line 25) is unrelated; add a new import near the top imports:

```python
from ..subagents.tiers import TIER_NAMES
```

Add the field to `AgentDef` (after `model: str | None = None`, line 79):

```python
    # The spec's declared model tier (cheap/med/high) for the native backend's
    # tier router. None ⇒ no label; the router falls back to tool reach. An
    # out-of-range value is normalized to None at parse time.
    tier: str | None = None
```

- [ ] **Step 4: Parse and validate `tier` in `_parse_agent`**

In `_parse_agent` (after the `model = _opt_str(...)` line, line 164), add:

```python
    tier = _opt_str(data.get("tier"), None)
    if tier not in TIER_NAMES:
        tier = None
```

Then add `tier=tier,` to the `AgentDef(...)` return (after `model=model,`, line 173).

- [ ] **Step 5: Guard against an import cycle**

Run: `uv run python -c "import marim_harness.workspace.agents"`
Expected: no output (clean import). `subagents/tiers.py` has no marim imports, so `workspace.agents → subagents.tiers` cannot cycle. If this errors with a circular import, STOP and report — do not add a lazy import to paper over it.

- [ ] **Step 6: Run the parse tests to verify they pass**

Run: `uv run pytest tests/test_agents.py -k tier -v`
Expected: PASS (3 passed).

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/workspace/agents.py tests/test_agents.py
git commit -m "feat(agents): parse tier: frontmatter into AgentDef"
```

---

## Task 3: Tier resolution in `SubagentRunner.build`

**Files:**
- Modify: `src/marim_harness/subagents/runner.py` (`__init__` gains `tiers`; `build` runs the resolver)
- Test: `tests/test_subagent_tiers.py` (add a `build`-level test with a stub runner)

**Interfaces:**
- Consumes: `resolve_tier` (Task 1), `SubagentTiers` (Task 1), `AgentDef.tier` (Task 2), `GATED_TOOLS` from `..tools.names`.
- Produces: `SubagentRunner.__init__(..., tiers: SubagentTiers | None = None)`; `build(..., tier: str | None = None)` — the model an agent runs on is now the tier-resolved id (slug override still honored, bounded to the allowlist when tiers are configured).

- [ ] **Step 1: Write the failing build-resolution test**

Add to `tests/test_subagent_tiers.py`. The test drives the pure decision by calling a small extracted helper (added in Step 3) so it needs no live model source:

```python
from marim_harness.config.model import SubagentTiers
from marim_harness.subagents.runner import _resolve_spawn_model_id


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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_subagent_tiers.py -k resolve_spawn_model_id -v`
Expected: FAIL — `ImportError: cannot import name '_resolve_spawn_model_id'`.

- [ ] **Step 3: Add the pure `_resolve_spawn_model_id` helper**

In `src/marim_harness/subagents/runner.py`, add these imports near the existing imports (top of file):

```python
from ..config.model import SubagentTiers
from ..tools.names import GATED_TOOLS
from .tiers import resolve_tier
```

Add this module-level pure helper (above the `SubagentRunner` class):

```python
def _resolve_spawn_model_id(
    override_tier: str | None,
    slug: str | None,
    spec_tier: str | None,
    read_only: bool,
    tiers: SubagentTiers,
) -> str | None:
    """The model id a spawn should run on, or None to inherit the main model.

    A raw ``slug`` override is the escape hatch: honored as-is when no tier is
    configured (legacy behavior, preserved), but bounded to the tier allowlist
    once tiers exist — an out-of-allowlist slug is dropped in favor of the
    tier-resolved model (the caller logs the drop). With no slug, the tier is
    resolved from ``override_tier`` → ``spec_tier`` → tool reach, and mapped to
    its configured model (None ⇒ inherit main). Pure; unit-tested directly."""
    allow = tiers.allowlist()
    if slug:
        if not allow or slug in allow:
            return slug
        # else: out-of-allowlist slug falls through to the tier default.
    name = resolve_tier(override_tier, spec_tier, read_only)
    return tiers.model_for(name)
```

- [ ] **Step 4: Run the helper test to verify it passes**

Run: `uv run pytest tests/test_subagent_tiers.py -k resolve_spawn_model_id -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Store `tiers` on the runner**

In `SubagentRunner.__init__` (signature around line 94), add a keyword parameter after `masking`:

```python
                 masking: MaskingPolicy | None = None,
                 tiers: SubagentTiers | None = None,
                 extra_agents: tuple[AgentDef, ...] = ()) -> None:
```

And in the body (near the other `self._` assignments, e.g. after `self._masking = ...`):

```python
        # The user-curated model per tier. Empty (the default) ⇒ every spawn
        # inherits the main model and a slug override keeps its legacy passthrough.
        self._tiers = tiers or SubagentTiers()
```

- [ ] **Step 6: Wire the resolver into `build`**

In `SubagentRunner.build` (lines 258-264), add the `tier` parameter:

```python
    def build(
        self, type: str, max_output_chars: int | None = None,
        model: str | None = None, workspace_root=None, *, defn=None,
        depth: int = 0, mask_trigger: int | None = None,
        checkpoint: Callable[[list], None] | None = None,
        output_schema: dict | None = None, tier: str | None = None,
    ) -> tuple[SubAgent | None, str | None]:
```

Replace the model-selection block (current lines 294-302):

```python
        if model is None:
            model_obj = self._get_model()
        elif self._build_model is None:
            return None, (
                f"Can't run sub-agent on model {model!r}: no model source is "
                "available to resolve an override here."
            )
        else:
            model_obj = self._build_model(model)
```

with tier-aware resolution:

```python
        read_only = not (defn.tools & GATED_TOOLS)
        model_id = _resolve_spawn_model_id(
            override_tier=tier, slug=model, spec_tier=defn.tier,
            read_only=read_only, tiers=self._tiers,
        )
        if model and model_id != model:
            logger.debug(
                "sub-agent slug override %r not in tier allowlist; using %r",
                model, model_id,
            )
        if model_id is None:
            model_obj = self._get_model()
        elif self._build_model is None:
            return None, (
                f"Can't run sub-agent on model {model_id!r}: no model source is "
                "available to resolve an override here."
            )
        else:
            model_obj = self._build_model(model_id)
```

Note: `defn` is guaranteed non-None here — the `if defn is None: return None, ...` guard at lines 283-290 runs first. `logger` is already module-level in this file. The `model_id != model` log fires only when the slug was dropped for being out of the allowlist (`_resolve_spawn_model_id` returned a tier model instead of the passed slug).

- [ ] **Step 7: Thread `tier` from `_prepare_spawn` into `build`**

In `_prepare_spawn` (signature around line 586), add `tier: str | None = None` to the keyword-only args:

```python
        *, debug: bool, t0: float, defn=None, depth: int = 0,
        resumed: bool = False, output_schema: dict | None = None,
        tier: str | None = None,
```

And pass it into the `self.build(...)` call (line 631):

```python
        sub, err = self.build(type, max_output_chars, model, work_root, defn=defn,
                              depth=depth, mask_trigger=mask_trigger,
                              checkpoint=checkpoint, output_schema=output_schema,
                              tier=tier)
```

- [ ] **Step 8: Thread `tier` from `_execute_spawn` into `_prepare_spawn`**

In `_execute_spawn` (signature around line 520), add `tier: str | None = None` to the keyword-only args:

```python
        *, background: bool, stream_id: str, caller_depth: int = 0,
        output_schema: dict | None = None, tier: str | None = None,
```

And pass it into the `_prepare_spawn(...)` call (line 575):

```python
        prep = await self._prepare_spawn(
            type, task, mcp_names, max_output_chars, model,
            iso, work_root, stream_id, debug=debug, t0=t0, defn=defn, depth=depth,
            output_schema=output_schema, tier=tier,
        )
```

- [ ] **Step 9: Run the full runner test module + typecheck**

Run: `uv run pytest tests/test_subagent_tiers.py -v && uv run pyright src/marim_harness/subagents/runner.py`
Expected: tests PASS; pyright reports no new errors in `runner.py`.

- [ ] **Step 10: Commit**

```bash
git add src/marim_harness/subagents/runner.py tests/test_subagent_tiers.py
git commit -m "feat(subagents): resolve spawn model by tier in build"
```

---

## Task 4: `run`/`run_background` service seam + `spawn_agent(tier=)`

**Files:**
- Modify: `src/marim_harness/subagents/runner.py` (`run`, `run_background` accept + forward `tier`)
- Modify: `src/marim_harness/runtime/deps.py` (`SubAgentRunner`, `BackgroundAgentRunner` aliases gain the tier slot)
- Modify: `src/marim_harness/tools/spawn_tools.py` (`spawn_agent` + `_spawn_background` `tier` param; docstring)
- Test: `tests/test_subagent_tiers.py` (a `run`-forwarding test)

**Interfaces:**
- Consumes: `_execute_spawn(..., tier=...)` (Task 3).
- Produces: `SubAgentRunner` / `BackgroundAgentRunner` type aliases with a trailing `str | None` tier arg; `spawn_agent(..., tier: str | None = None)`.

- [ ] **Step 1: Read the current `run` / `run_background` signatures**

Run: `uv run python - <<'PY'
import inspect, marim_harness.subagents.runner as m
src = inspect.getsource(m.SubagentRunner.run)
print(src)
print(inspect.getsource(m.SubagentRunner.run_background))
PY`
Expected: prints both method bodies (lines ~757-820) so you thread `tier` in exactly, matching their existing parameter order.

- [ ] **Step 2: Add `tier` to `run` and `run_background`**

In `src/marim_harness/subagents/runner.py`, `run` (around line 757) — add `tier: str | None = None` as the final parameter and forward it to the `_execute_spawn` call (around line 788):

```python
    async def run(
        self, type: str, task: str, stream_id: str,
        mcp_names: list[str] | None = None, max_output_chars: int | None = None,
        model: str | None = None, isolation: str | None = None,
        caller_depth: int = 0, output_schema: dict | None = None,
        tier: str | None = None,
    ) -> str:
        ...
        return await self._execute_spawn(
            type, task, mcp_names, max_output_chars, model, isolation,
            background=False, stream_id=stream_id, caller_depth=caller_depth,
            output_schema=output_schema, tier=tier,
        )
```

Do the same for `run_background` (around line 793 → `_execute_spawn` call around line 817), adding `tier: str | None = None` and `tier=tier` to the delegated call. **Preserve the exact existing parameter order and any params not shown here — only add the trailing `tier` and forward it.**

- [ ] **Step 3: Update the service type aliases**

In `src/marim_harness/runtime/deps.py`, extend both Callable aliases with a trailing `str | None` (tier) argument.

`SubAgentRunner` (lines 31-35) — the current arg tuple is
`[str, str, str, list[str] | None, int | None, str | None, str | None, int]`
(type, task, stream_id, mcp_names, max_output_chars, model, isolation, caller_depth). Note `run` also has `output_schema` and now `tier` as trailing keyword-or-positional args; the alias models the positional call shape used by callers. Add `str | None` for tier:

```python
SubAgentRunner = Callable[
    [str, str, str, list[str] | None, int | None, str | None,
     str | None, int, str | None],
    Awaitable[str],
]
```

`BackgroundAgentRunner` (lines 58-61) — current tuple
`[str, str, list[str] | None, int | None, str | None, str | None, str, int]`
(type, task, mcp_names, max_output_chars, model, isolation, stream_id, caller_depth). Add the tier slot:

```python
BackgroundAgentRunner = Callable[
    [str, str, list[str] | None, int | None, str | None, str | None, str, int,
     str | None],
    Awaitable[str],
]
```

Update the comment above `BackgroundAgentRunner` (lines 52-57) to name `tier` in the arg list.

- [ ] **Step 4: Add `tier` to `spawn_agent` and thread it**

In `src/marim_harness/tools/spawn_tools.py`, add the parameter to `spawn_agent` (after `model`, line 229):

```python
    model: str | None = None,
    tier: str | None = None,
    isolation: str | None = None,
```

Thread it into the foreground service call (the `run_subagent` call, lines 352-355):

```python
    return await ctx.deps.services.run_subagent(
        type, task, ctx.tool_call_id or "", mcp_names, max_output_chars, model,
        isolation, ctx.deps.subagent_depth, tier,
    )
```

Note `run_subagent` is `SubagentRunner.run`; its positional order is
`(type, task, stream_id, mcp_names, max_output_chars, model, isolation, caller_depth, [output_schema], tier)`. Because `output_schema` sits before `tier` and is keyword-defaulted, pass `tier` by keyword to be safe:

```python
    return await ctx.deps.services.run_subagent(
        type, task, ctx.tool_call_id or "", mcp_names, max_output_chars, model,
        isolation, ctx.deps.subagent_depth, tier=tier,
    )
```

(If the static `SubAgentRunner` Callable type rejects the keyword form under pyright, keep the alias positional and instead ensure `run`'s signature places `tier` immediately after `caller_depth` with `output_schema` last — pick whichever ordering makes the one positional call in this file and the resume call site in `runner.py`/screens consistent. Verify with `uv run pyright` in Step 7.)

- [ ] **Step 5: Thread `tier` through `_spawn_background`**

In `_spawn_background` (definition around line 150; the `run_background_agent` call sites at lines 192-195 and 206-209), add `tier: str | None = None` to its signature and pass `tier` as the final arg to both `run_bg(...)` / `ctx.deps.services.run_background_agent(...)` calls. Then pass `tier=tier` from `spawn_agent`'s `_spawn_background(...)` call (lines 335-346):

```python
        return await _spawn_background(
            ctx,
            type=type,
            task=task,
            description=description,
            mcp_names=mcp_names,
            after_ids=after_ids,
            max_output_chars=max_output_chars,
            auto_detached=auto_detached,
            model=model,
            isolation=isolation,
            tier=tier,
        )
```

The two background service calls become (final positional arg added):

```python
            return run_bg(
                type, full_task, mcp_names, budget, model, isolation,
                ctx.tool_call_id or "", ctx.deps.subagent_depth, tier,
            )
```

and

```python
            ctx.deps.services.run_background_agent(
                type, task, mcp_names, budget, model, isolation,
                ctx.tool_call_id or "", ctx.deps.subagent_depth, tier,
            ),
```

- [ ] **Step 6: Rewrite the model/tier docstring block**

Replace the `model` paragraph in `spawn_agent`'s docstring (lines 293-300) with a tier-first version:

```python
    `tier` routes this spawn to one of your configured model tiers — `"cheap"`,
    `"med"`, or `"high"` — instead of your own model. Prefer this over `model`:
    pass `"cheap"` for read-only fan-out where a small model suffices, `"high"`
    for a hard sub-task. Omit it and the spawn takes its automatic tier (a
    read-only agent defaults to cheap, a workspace-mutating one to high; a custom
    agent may pin its own tier). A tier with no model configured falls back to
    your current model, so `tier` is always safe to pass.

    `model` is an advanced escape hatch: it names a specific model id to run this
    spawn on, bounded to your configured tier models. Prefer `tier` — reach for
    `model` only when you need an exact model the tiers don't cover. For a
    sub-agent whose definition sets `backend: claude-cli`, `model` is a Claude
    Code model name (e.g. `opus`, `sonnet`, a full id) passed straight to the
    CLI; `tier` does not apply to claude-cli spawns. Omit both to inherit your
    current model (the usual case).
```

- [ ] **Step 7: Write a forwarding test, run it, and typecheck**

Add to `tests/test_subagent_tiers.py` a test that `run` forwards `tier` into `_execute_spawn` (monkeypatch `_execute_spawn` to capture kwargs). Sketch:

```python
import asyncio


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
            runner, "explore", "task", "sid", None, None, None, None, 0, None, "cheap"
        )
    )
    assert out == "ok"
    assert captured.get("tier") == "cheap"
```

(If `run` has intervening logic before `_execute_spawn` that touches `self`, adjust the stub to satisfy it, or assert via a thinner seam — the point is proving `tier` reaches `_execute_spawn`.)

Run: `uv run pytest tests/test_subagent_tiers.py -v && uv run pyright src/marim_harness/tools/spawn_tools.py src/marim_harness/runtime/deps.py src/marim_harness/subagents/runner.py`
Expected: tests PASS; no new pyright errors.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/subagents/runner.py src/marim_harness/runtime/deps.py src/marim_harness/tools/spawn_tools.py tests/test_subagent_tiers.py
git commit -m "feat(subagents): spawn_agent tier param threaded to the runner"
```

---

## Task 5: Wire tiers config into the harness build

**Files:**
- Modify: `src/marim_harness/runtime/harness.py` (`HarnessConfig.subagent_tiers`; pass `tiers=` to `SubagentRunner`)
- Modify: `src/marim_harness/runtime/bootstrap.py` (pass `subagent_tiers=cfg.subagent.tiers`)
- Test: `tests/test_bootstrap.py` (assert the runner receives the tiers)

**Interfaces:**
- Consumes: `SubagentConfig.tiers` (Task 1), `SubagentRunner(tiers=...)` (Task 3).
- Produces: `HarnessConfig.subagent_tiers: SubagentTiers | None`.

- [ ] **Step 1: Write the failing wiring test**

Add to `tests/test_bootstrap.py` (follow the file's existing `build_harness`/monkeypatch setup — reuse whatever fixture already builds a harness there). Assert the constructed runner carries the env-configured tiers:

```python
def test_build_harness_threads_subagent_tiers(monkeypatch, tmp_path):
    monkeypatch.setenv("MARIM_SUBAGENT_TIER_CHEAP", "openrouter:some/cheap-model")
    # ... reuse this module's standard build_harness invocation/fixture ...
    harness = _build_test_harness(tmp_path)  # replace with the file's helper
    assert harness._subagents._tiers.cheap == "openrouter:some/cheap-model"
```

(Use the module's existing harness-construction helper and the attribute it already uses to reach the runner; `_subagents`/`subagents` naming should match what other tests in the file use.)

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_bootstrap.py -k subagent_tiers -v`
Expected: FAIL — tiers empty (not threaded yet).

- [ ] **Step 3: Add the `HarnessConfig` field**

In `src/marim_harness/runtime/harness.py`, import `SubagentTiers` (it already imports from `..config.model`; extend that import or add):

```python
from ..config.model import SubagentTiers
```

Add the field to `HarnessConfig` (near `model_source`, line 114):

```python
    subagent_tiers: SubagentTiers | None = None
```

- [ ] **Step 4: Pass `tiers=` into `SubagentRunner`**

In `build_collaborators` where `SubagentRunner(...)` is constructed (lines 346-375), add before `build_model=`:

```python
        tiers=cfg.subagent_tiers,
```

- [ ] **Step 5: Thread it in bootstrap**

In `src/marim_harness/runtime/bootstrap.py`, in the `with_config_overrides(...)` call (near line 183 where `subagent_concurrency=cfg.subagent.concurrency`), add:

```python
            subagent_tiers=cfg.subagent.tiers,
```

- [ ] **Step 6: Run the wiring test + typecheck the build path**

Run: `uv run pytest tests/test_bootstrap.py -k subagent_tiers -v && uv run pyright src/marim_harness/runtime/harness.py src/marim_harness/runtime/bootstrap.py`
Expected: PASS; no new pyright errors. (If `with_config_overrides(**fields)` rejects the key, confirm `HarnessConfig` has the `subagent_tiers` field from Step 3 — the builder forwards **fields straight into `HarnessConfig(**config_fields)`.)

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/runtime/harness.py src/marim_harness/runtime/bootstrap.py tests/test_bootstrap.py
git commit -m "feat(runtime): thread subagent tiers config into the runner"
```

---

## Task 6: TUI tier pickers in Settings

**Files:**
- Modify: `src/marim_harness/interfaces/tui/settings.py` (three tier pickers)
- Test: `tests/test_settings_screen.py`

**Interfaces:**
- Consumes: `ModelPickerModal` + catalog fetchers (existing), `save_env_settings` + `refresh_from_env` (existing, used by Providers), `harness.model_source` (existing).
- Produces: three settings rows that write `MARIM_SUBAGENT_TIER_CHEAP/_MED/_HIGH` and apply live.

- [ ] **Step 1: Read the existing settings model-picker + save path**

Run: `grep -n "ModelPickerModal\|save_env_settings\|refresh_from_env\|set_model\|MARIM_" src/marim_harness/interfaces/tui/settings.py`
Expected: shows how Settings opens the picker and persists env (lines ~658-669 per the design notes) so the tier rows mirror that exact pattern — three rows, each opening `ModelPickerModal(fetch=source.list_models, ...)`, on-choose writing its env var via `save_env_settings({...})` then `source.refresh_from_env()`.

- [ ] **Step 2: Write the failing settings test**

Add to `tests/test_settings_screen.py`, matching the file's existing Textual `run_test()`/pilot harness. Assert: (a) three tier rows render with labels "Cheap tier"/"Med tier"/"High tier"; (b) choosing a model for the cheap row calls `save_env_settings` with `MARIM_SUBAGENT_TIER_CHEAP` set to the chosen qualified id. Reuse the module's existing settings-screen fixture and its monkeypatch of `save_env_settings`.

```python
async def test_settings_has_three_tier_rows(settings_app):
    async with settings_app.run_test() as pilot:
        screen = pilot.app.screen
        labels = {w.renderable.plain for w in screen.query(".tier-row-label")}
        assert {"Cheap tier", "Med tier", "High tier"} <= labels
```

(Adjust selectors/label plumbing to the file's conventions; the assertion targets the three rows existing and being labeled.)

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/test_settings_screen.py -k tier -v`
Expected: FAIL — rows/labels absent.

- [ ] **Step 4: Add the three tier rows**

In `src/marim_harness/interfaces/tui/settings.py`, add a "Sub-agent model tiers" section rendering three rows (Cheap/Med/High), each with a button showing the current env value (or "inherit main" when unset) that opens `ModelPickerModal`. On selection, persist and apply live:

```python
    def _on_tier_chosen(self, tier_env: str, chosen: str | None) -> None:
        if chosen is None:
            return
        save_env_settings({tier_env: chosen})
        source = self.harness.model_source
        if source is not None and hasattr(source, "refresh_from_env"):
            source.refresh_from_env()
        # Refresh the row's button label to the chosen id.
        self._refresh_tier_labels()
```

Wire each row's button to `ModelPickerModal(current=<current env value>, fetch=source.list_models, is_local=source.is_local)` and dismiss into `_on_tier_chosen("MARIM_SUBAGENT_TIER_CHEAP", chosen)` (and `_MED` / `_HIGH`). Follow the exact modal-open + dismiss pattern the existing main-model picker in this file uses (Step 1). Give each label the `tier-row-label` class and text "Cheap tier"/"Med tier"/"High tier" to match the test.

Note: live-applying the tier env updates `.env` and the `MultiModelSource`, but the running `SubagentRunner._tiers` was captured at build time. For v1, a changed tier applies to **sub-agents spawned after the next harness rebuild / session relaunch** — the picker persists the choice and refreshes the model catalog immediately; document this in the row's help text ("applies to new sessions"). (A live `harness.set_subagent_tiers()` setter mirroring `set_workflows_enabled()` is a deferred follow-up, out of scope here.)

- [ ] **Step 5: Run the settings test + full TUI settings suite**

Run: `uv run pytest tests/test_settings_screen.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/settings.py tests/test_settings_screen.py
git commit -m "feat(tui): sub-agent model tier pickers in settings"
```

---

## Task 7: Docs + full-suite gate

**Files:**
- Modify: `.env.example` (document the three tier env vars)
- Modify: `CLAUDE.md` (one line under the `subagents/` or `workflows/` architecture notes)

- [ ] **Step 1: Document the env vars**

Add to `.env.example` under the sub-agent section:

```bash
# Sub-agent model tiers: the model each tier routes to (qualified provider:model_id).
# A read-only spawn defaults to cheap, a mutating one to high; a spec's `tier:` or
# the spawner's tier= override picks explicitly. Unset ⇒ inherit the main model.
# MARIM_SUBAGENT_TIER_CHEAP=local:ornith-1.0-9b
# MARIM_SUBAGENT_TIER_MED=openrouter:anthropic/claude-sonnet-4-6
# MARIM_SUBAGENT_TIER_HIGH=openrouter:anthropic/claude-opus-4
```

- [ ] **Step 2: Note it in CLAUDE.md**

Add one sentence to the `subagents/` bullet in CLAUDE.md's architecture section:

> Native spawns pick a model by **tier** (`cheap`/`med`/`high`, in `subagents/tiers.py`): resolved from the spawner's `tier=` override → the spec's `tier:` frontmatter → tool reach (read-only→cheap, mutating→high), mapped to `MARIM_SUBAGENT_TIER_*`; unset tiers inherit the main model and a `model=` slug stays a bounded escape hatch.

- [ ] **Step 3: Run the full gate**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: ruff clean, pyright clean, full suite PASS. Fix anything red before committing.

- [ ] **Step 4: Commit**

```bash
git add .env.example CLAUDE.md
git commit -m "docs: sub-agent model tiers env vars + architecture note"
```

---

## Self-Review Notes (for the implementer)

- **Spec coverage:** Task 1 (config + resolver), Task 2 (frontmatter), Task 3 (native `build` resolution), Task 4 (override-by-tier param + allowlist-bounded slug), Task 5 (wiring), Task 6 (TUI), Task 7 (docs) cover every section of the design. CLI is explicitly out of scope for tier routing (the spec's "tier does not apply to claude-cli spawns" — `cli_spawn.py` keeps `defn.model`/`model` slug), so no CLI code changes here.
- **Backward compat:** empty tiers → inherit main + legacy slug passthrough (`_resolve_spawn_model_id` Steps 3/tests). "Fresh install behaves like today" is preserved literally.
- **Type consistency:** `resolve_tier(override, spec_tier, read_only)` and `_resolve_spawn_model_id(override_tier, slug, spec_tier, read_only, tiers)` names are used identically across Tasks 1/3. `SubagentTiers.model_for` / `.allowlist` names match across config/runner/tests.
- **Deferred (documented, not silently dropped):** live re-application of a tier change to a running runner (Task 6 Step 4 note); a tiny classifier tier; per-provider tiers; CLI-side tier→model mapping.
