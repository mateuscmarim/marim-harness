# Sub-agent Model Tiers — Design

**Date:** 2026-07-13
**Status:** Approved

## Problem

Today every sub-agent runs on whatever model the main agent is on. The main
model *can* pass a per-spawn `model=` slug to `spawn_agent` (the plumbing is
wired end-to-end), but nothing curates which models are permitted, nothing sets
a cheaper default for cheap work, and the native backend ignores the `model:`
field that sub-agent specs already parse. The result: fan-out that could run on
a cheap model runs on the expensive main model instead, and the user has no knob
to control it.

We want the user to curate a small set of models usable by sub-agents and have
the harness route each spawn to a sensible one automatically — while still
letting the main model exercise judgment when it has it.

## Decisions (from brainstorming)

- **Shape:** a **deterministic default tier** applies automatically; the main
  model **may override per-spawn**, bounded to the curated set. Not pure
  automatic routing (too coarse) and not "model decides freely" (no enforceable
  savings — LLMs under-comply at downgrading themselves). This hybrid gives
  cheap-by-default without relying on model compliance, plus the model's
  judgment when it exercises it.
- **Three tiers:** `cheap` / `med` / `high`. Each is backed by a user-chosen
  model. The three tier models (plus the main model) *are* the allowlist —
  there is no separate list to maintain.
- **Routing signal — precedence, highest wins:**
  1. **Main-model override** — the main model names a tier when spawning
     (`tier="cheap"`).
  2. **Spec label** — `tier: med` in the sub-agent markdown frontmatter.
  3. **Tool-reach default** — read-only fan-out (no write/edit/bash) → `cheap`;
     workspace-mutating agents → `high`.
  - `med` is deliberately the opt-in middle: reachable via a spec label or a
    model override, never from tool reach alone.
- **Override vocabulary:** the main model overrides **by tier name**, not a raw
  model slug — it reasons about difficulty, never about model IDs, keeping its
  prompt stable and the allowlist automatic. The existing `spawn_agent(model=)`
  slug path stays as an advanced escape hatch, validated against the allowlist.
- **Safe by default:** any tier left unset falls back to the **main** model, so
  a fresh install behaves exactly like today.
- **Failure handling:** an unknown tier name or an unapproved slug **falls back
  to the resolved default and logs** — never hard-fails a spawn.

## Design

### Tier configuration & the allowlist

Three tier slots, each holding a qualified `provider:model_id` (or empty →
main). Built through the existing `MultiModelSource`, exactly like the main
model, so tier models can span any active provider.

- **Config source:** `MARIM_SUBAGENT_TIER_CHEAP` / `_MED` / `_HIGH` env vars for
  headless and `.env`, surfaced and editable in the TUI settings. Saving goes
  through the existing `save_env_settings` + live `refresh_from_env` path that
  Providers settings already uses, so edits apply without relaunch.
- **Allowlist = {main, cheap, med, high}.** No separate list is stored; the tier
  map defines the permitted set. Empty tiers collapse to main.
- A new value object `SubagentTiers` (mapping tier name → optional qualified id)
  lives in `config/model.py` next to `SubagentConfig`, populated from env in the
  config loader. It is a plain immutable dataclass — pure, unit-tested directly.

### Routing — the resolver

The core new logic is a **pure** function, side-effect-free and unit-tested per
the repo's pure-helper convention:

```
resolve_tier(override: str | None,
             defn_tier: str | None,
             read_only: bool) -> str        # returns "cheap" | "med" | "high"
```

Precedence: `override` → `defn_tier` → (`read_only` ? "cheap" : "high").
Unknown/invalid names are ignored at their level (fall through) — the caller
logs when it drops an out-of-range value. `resolve_tier` returns a tier *name*;
mapping name → model id (and the empty-tier → main fallback) is a second tiny
pure step (`tier_model(tiers, name, main_id)`), keeping "which tier" and "which
model backs it" separate and independently testable.

`read_only` is derived from the sub-agent's granted tool set (the same
`defn.tools` split that already distinguishes explore-type from mutating
agents — no write/edit/bash ⇒ read-only).

### Where it plugs in

- **`workspace/agents.py`** — `AgentDef` gains a `tier: str | None` field;
  `_parse_agent` reads `tier` from frontmatter next to the existing `model`.
- **`subagents/runner.py`** — `SubagentRunner.build` is the core change. Today
  the native path ignores `defn.model` and only reads the spawn `model` param.
  It now: computes `read_only` from the granted tools, calls `resolve_tier` +
  `tier_model`, and builds the resulting id via the existing `_build_model`
  closure (which wraps `MultiModelSource.build`). A raw-slug override still
  works but is validated against the allowlist first; an unapproved slug logs
  and falls back to the tier-resolved model.
- **`tools/spawn_tools.py`** — `spawn_agent` gains a `tier` parameter
  (`"cheap" | "med" | "high" | None`). Its docstring — model-facing product
  copy — is rewritten to teach the tier vocabulary and when to downgrade
  (cheap for read-only fan-out) or escalate.
- **`subagents/cli_spawn.py`** — already honors `defn.model`. Both backends
  share the same **tier-resolution policy** (`resolve_tier`), but the concrete
  model a tier maps to is backend-specific: native resolves to a qualified
  `provider:model_id` built via `MultiModelSource`, while the CLI path resolves
  to a Claude Code model name (`sonnet`/`opus`/`haiku`) passed to `--model`.
  So the tier config carries a native id and, for CLI spawns, `defn.model` /
  the CLI model env continue to supply the CLI-side name — one resolver, two
  model vocabularies, not two policies.
- **TUI settings** — three tier pickers reusing `ModelPickerModal` + the
  existing catalog fetchers, living alongside the Providers pane that already
  holds the live `MultiModelSource`. Selecting a model writes the corresponding
  `MARIM_SUBAGENT_TIER_*` env and refreshes live.

### Interaction with existing behavior

- **Workflows** (`agent()` host fn) spawn through `SubagentRunner.run`, so they
  inherit the same resolver automatically; a workflow's `model=` override maps
  onto the slug escape hatch (allowlist-validated).
- **Nested spawns** resolve their tier the same way at each level; depth is not
  a routing input in v1 (kept in reserve as a possible future nudge).
- **`claude-cli` provider main loop** is unaffected — it delegates the whole
  turn to the CLI; tiers apply to marim-native and `backend: claude-cli` spawns.

## Non-goals (YAGNI)

- No LLM-based difficulty classifier hop (adds latency + per-spawn
  nondeterminism; the reach + override policy is the cheaper starting point and
  the same seam admits a classifier later).
- No depth/prompt-size heuristics in v1.
- No per-provider tier fan-out (a single model per tier; it may itself be on any
  provider).
- No persisted per-session tier overrides — tiers are global config like the
  provider credentials.

## Testing

- **Pure precedence table** for `resolve_tier`: override > spec > reach; unknown
  names fall through; `read_only` maps to cheap, mutating to high.
- **`tier_model`**: name → id, empty tier → main fallback, allowlist membership.
- **Frontmatter parse**: `tier:` read into `AgentDef`; absent → `None`.
- **Native-spawn wiring**: a spawn with a given tier/spec reaches `build_model`
  with the expected qualified id (and an unapproved slug falls back + logs).
- **Settings screen**: the three tier pickers render, and choosing a model
  writes the right `MARIM_SUBAGENT_TIER_*` env and refreshes live.

## Open confirms (locked to these defaults unless vetoed on review)

- Mutating spawns default to **high** (not med).
- Main-model override is **by tier name**; raw-slug override is a bounded
  escape hatch, not the primary path.
