# Native deep-research for marim — design

**Date:** 2026-06-26
**Status:** Approved (design), pending implementation plan

## Problem

A plain research prompt to marim runs entirely in the main turn loop — one agent
doing sequential web lookups — and never fans out via `spawn_agent`. The fan-out
machinery exists (`spawn_agent` with built-in `explore`/`general` types, plus the
`web_search`/`fetch_url` tools), but it is **opt-in**: the model only delegates when
the task tells it to. marim ships no skill that encodes that orchestration, so the
behavior the user expected from "deep research" (parallel sub-agents, source
verification, a synthesized cited report) simply doesn't happen.

The `/deep-research` skill that produces that behavior elsewhere is a *Claude Code*
feature, not a marim one. This design ports the capability into marim natively.

## Goal

Ship a **built-in `deep-research` skill** so that, when the user asks for research,
the *main* agent fans out into parallel workers, adversarially verifies the
load-bearing claims, and synthesizes one cited report. Ship it inside the repo so
every marim install has it, it is version-controlled, and it is covered by CI.

Full-pipeline fidelity (not a lean fan-out): **plan → fan-out search → adversarial
claim-verification → synthesize**.

## Key constraints (from the codebase)

- **Only the main agent can fan out.** `spawn_agent` is never granted to sub-agents,
  so they cannot recurse (`tools/provider.py`, CLAUDE.md). Therefore the orchestration
  must live where the main agent runs — i.e. a **skill**, not a sub-agent definition.
  A deep-research *agent* could not spawn workers.
- **Skills are model-invoked.** `discover_skills` injects a one-line `name — description`
  index into the system prompt each turn; the model loads a skill body on demand via
  the main-agent-only skill tool (`workspace/skills.py`, `runtime/instructions.py`).
- **Skills and agents load only from project + global roots today.** Both
  `skill_roots()` and `agent_roots()` return `[("project", .marim/...), ("global",
  config_dir()/...)]`. There is no package/built-in root. Plugins also load only from
  project/global dirs, so a "bundled plugin" would still need a new root or an install
  step — more moving parts for the same result.
- **Discovery dedups first-root-wins**, sorted for stable display
  (`discover_skills`/`discover_agents` via `cached_discover`).
- **Workers' reach is set by type up front.** `explore` already grants
  `READ_TOOLS | NET_TOOLS` and nothing mutating (`workspace/agents.py`). A research
  worker is `explore` with a tuned prompt.

## Design

Two markdown assets shipped in the package, plus a small built-in discovery root that
makes them visible, plus tests.

### 1. Built-in discovery root (the mechanism)

Add a third root to **both** `skill_roots()` and `agent_roots()`, placed **after**
`global`:

```python
def skill_roots(workspace_root):
    ws = Path(workspace_root)
    return [
        ("project", ws / ".marim" / "skills"),
        ("global", config_dir() / "skills"),
        ("builtin", _builtin_root() / "skills"),
    ]
# agent_roots(): identical, with "agents"
```

`_builtin_root()` resolves package-relative: from `workspace/skills.py`,
`Path(__file__).resolve().parent.parent / "builtin"` → `src/marim_harness/builtin`.
A single shared helper (e.g. in `workspace/` or `config.py`) avoids duplicating the
path math across the two modules.

**Precedence becomes project > global > builtin > plugins.** Because discovery is
first-root-wins, a user can shadow the bundled skill/agent by dropping their own
`deep-research/` (or `researcher.md`) into `.marim/skills` / `.marim/agents`. Plugins
remain lowest. `Skill.source` / `AgentDef.source` carry the literal `"builtin"`.

No change to the discovery/caching internals — the new root flows through the existing
`_all_skill_roots` / `cached_discover` path unchanged. The stat-based discovery
signature already fingerprints every root, so the built-in dir is cache-correct for
free.

### 2. Files shipped in the repo

```
src/marim_harness/builtin/
  skills/
    deep-research/
      SKILL.md          # orchestration policy, run by the MAIN agent
  agents/
    researcher.md       # tuned read-only web worker (a sub-agent type)
```

### 3. `researcher` agent (`builtin/agents/researcher.md`)

Frontmatter:

```yaml
---
description: Web research worker — gathers sourced findings on one sub-question, read-only.
tools: web_search, fetch_url, read_file, glob, grep, tree
---
```

The tool list is a **minimal web-research set**: web egress plus basic local reads,
nothing mutating, no `spawn_agent` (so it can't recurse). (As-built note: an earlier
draft said this "mirrors `explore`'s reach, `READ_TOOLS | NET_TOOLS`". It does not —
`READ_TOOLS` also includes the six `LSP_TOOLS`, which a web worker has no use for. The
shipped agent lists exactly the six tools above; see commit `dcfa529`.) Body (the
worker's system prompt) encodes:

- **Source hierarchy:** systematic reviews / meta-analyses > RCTs > observational >
  everything else. Explicitly down-weight vendor pages, press releases, SEO blogs, and
  flag any claim that only traces back to those.
- **Recency bias** toward recent primary work, but keep landmark older sources that
  still anchor the field.
- **Output contract:** return findings as a list of claims; each claim carries a source
  URL, a source-type tag (meta-analysis / RCT / observational / other), and a
  one-word quality flag. Lead with the conclusion; note gaps and contradictions found.

### 4. `deep-research` skill (`builtin/skills/deep-research/SKILL.md`)

Frontmatter `name: deep-research` + a `description` written so the model invokes it
whenever the user wants a multi-source, fact-checked research report. Body = the
full pipeline as instructions to the **main** agent:

1. **Plan.** Decompose the question into independent sub-questions. If the question is
   underspecified for research (no scope/constraints), ask 1–3 clarifying questions
   first, then proceed.
2. **Fan out.** In a **single turn**, `spawn_agent type=researcher` once per
   sub-question (parallel), each with a `returns` contract matching the worker's output
   shape and `context` explaining the overall question. Explicit instruction:
   **do NOT research inline — delegate.** (This is the exact gap that made the first
   test produce zero sub-agents.)
3. **Adversarial verify.** For the load-bearing claims from the workers' reports,
   `spawn_agent type=explore` skeptics prompted to *refute* — find counter-evidence,
   check that the cited source actually supports the claim. Drop or downgrade claims
   that don't survive.
4. **Synthesize.** One cited report: every nontrivial claim keeps its citation; where
   sources genuinely disagree, say so and explain why (effect size / trial quality /
   population) rather than flattening to a verdict. End with a short "established vs.
   hyped" summary and a per-sub-question confidence rating with the main limiting
   factor.

The body includes a brief worked example (the creatine-cognition topic) so the model
sees the intended fan-out shape.

### 5. Packaging

Build backend is hatchling with `[tool.hatch.build] include = ["src"]`. Non-`.py`
files under the package are expected to ship, but this **must be verified**: build the
wheel and confirm `builtin/skills/deep-research/SKILL.md` and
`builtin/agents/researcher.md` are present. If hatchling drops them, add an explicit
`force-include` / `artifacts` rule for `src/marim_harness/builtin/**`.

### 6. Testing

Unit tests (no network — we test discovery/wiring, not live research):

- Built-in `deep-research` skill is discovered; `source == "builtin"`; it appears in
  the skill index text.
- Built-in `researcher` agent is discovered; `source == "builtin"`; its effective
  tools are read-only + net (no gated/mutating tools, no `spawn_agent`).
- **Shadowing:** a project (and global) `deep-research` skill / `researcher` agent
  wins over the built-in (precedence project > global > builtin).
- Built-in root flows through the discovery cache without breaking existing behavior
  (existing discovery tests still pass).

## Scope guard (YAGNI)

- No new config flags or env vars.
- No Python orchestrator / hardcoded fan-out — orchestration is the skill body,
  consistent with marim's model-driven design.
- No changes to `spawn_agent`, MCP, or the turn loop.
- Net change: two markdown assets + one shared `_builtin_root()` helper + ~3 lines in
  each of `skill_roots`/`agent_roots` + package-data verification + tests.

## Out of scope / future

- A live end-to-end research smoke test (costs tokens + network) — left to manual
  verification with a real model.
- Tuning worker count or adding a "completeness critic" pass — the skill body can grow
  these later without code changes.
