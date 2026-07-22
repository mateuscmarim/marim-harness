# Thinking Level — Design

**Status:** approved design, pending implementation plan
**Date:** 2026-07-22

## Goal

Let the user choose a **thinking level** (reasoning effort) that marim applies to
the model via pydantic-ai's `ModelSettings.thinking`, controllable from the TUI
(a `/think` slash command + a settings-screen row) and persisted per session.
The level also flows to sub-agents, with per-sub-agent override — full parity
with how Claude Code's `/effort` inherits to Task sub-agents with a frontmatter
override.

## Background

Today marim only *renders* thinking output a model happens to emit; it never
sets a thinking budget/effort. The only `ModelSettings` it builds is
`_DEFAULT_MODEL_SETTINGS = ModelSettings(parallel_tool_calls=True)`
(`runtime/harness.py:81`), applied at agent construction. pydantic-ai already
exposes everything needed:

- `pydantic_ai.settings.ModelSettings.thinking: ThinkingLevel`, where
  `ThinkingLevel = bool | Literal['minimal','low','medium','high','xhigh']`.
- `pydantic_ai.profiles.ModelProfile.supports_thinking: bool` (per-model
  capability flag; the unified `thinking` setting is ignored when `False`).

The **advisor** feature is the structural template for a session-persisted,
TUI-controlled, env-seeded knob; the **model tier** mechanism is the template
for sub-agent resolution (spawn override → spec frontmatter → inherited).

**Dependency:** no version bump required. The pinned `pydantic-ai-slim>=2.8,<3`
(installed 2.8.0) already exposes `ModelSettings.thinking`, `ThinkingEffort`
(`minimal/low/medium/high/xhigh`), and `ModelProfile.supports_thinking`.

## Levels

Single ordered source of truth:

```
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")
```

- `off` → **omit** the `thinking` key from `ModelSettings` (byte-identical to
  today's behavior; fully backward-compatible). Not an explicit `thinking=False`
  hard-disable.
- any other level → set `thinking="<level>"`.

Vocabulary is pydantic-ai's `ThinkingEffort` (note: has `minimal`, no `max`),
not Claude Code's (`low..max`). The mapping is deliberate — we set the
pydantic-ai unified setting, which providers translate
(`anthropic_thinking` / `openai_reasoning_effort`).

## Architecture

A new pure value module + a mirror of the advisor's session/config/harness/TUI
plumbing + a mirror of the tier resolver for sub-agents. No model or agent
rebuild is needed to change the level: the main loop reads the current level at
turn assembly and passes a per-run `model_settings`; sub-agents resolve at spawn.

### 1. Core value module (pure, unit-tested directly)

New module (e.g. `src/marim_harness/thinking.py`, root, next to `advisor.py`):

- `THINKING_LEVELS: tuple[str, ...]` — canonical set.
- `parse_thinking_level(s: str | None) -> str | None` — case-insensitive coerce;
  returns a canonical level, `"off"`, or `None` for unrecognized input (mirrors
  tier normalization at `workspace/agents.py:178-181`).
- `settings_for(level: str | None, base: ModelSettings) -> ModelSettings` —
  returns `base` unchanged for `off`/`None`; else `{**base, "thinking": level}`.
- `resolve_thinking(override, spec, inherited) -> str | None` — precedence
  **spawn override → spec frontmatter → inherited session level**; unrecognized
  candidates fall through (mirrors `subagents/tiers.py:resolve_tier`). Returns a
  level or `off`/`None` (no static default — the tail of the chain is the
  inherited session value).

### 2. Persistence (mirror advisor)

`session/store.py`:
- `SessionStore.thinking: str | None` + `SessionInfo.thinking` — tri-state like
  `advisor_model` (`store.py:152,162,174`): a level / `"off"` / `None`
  (unset → inherit `MARIM_THINKING`).
- Serialize in `save()` (~`store.py:186`), `save_meta()` (~248), and read in
  `load()` (~360, 381/385).
- `SessionManager.create()` inherits `latest_thinking()` (mirror
  `latest_advisor_model()` at `store.py:442-445`, applied at 421-422).

`session/ctrl.py`:
- `saved_thinking_id` property (mirror `saved_advisor_id`, 318-322).
- `set_thinking(value)` — `save_meta()` if the file exists else `persist(force=True)`
  (mirror `set_advisor`, 324-333: mid-turn-safe metadata patch).

### 3. Config / env

`config/model.py`:
- `ModelConfig.thinking_level: str | None = None` (near `advisor_model`, ~194).
- Env parse `MARIM_THINKING` (~301-305), validated via `parse_thinking_level`.

`runtime/bootstrap.py` — pass `thinking_level=cfg.thinking_level` into the
builder overrides (mirror `advisor_model` at ~187).

`runtime/builder.py` — `with_thinking(level)` → `with_config_overrides(thinking_level=level)`
(mirror `with_advisor`, 163-175).

### 4. Live seam / harness (mirror advisor, minus the tool)

`runtime/harness.py`:
- `self.thinking_level_id` current value; `self._thinking_env_default`.
- `set_thinking_level(self, level, *, persist=True)` — set live value, persist
  via `self.session.set_thinking(level or "off")` (mirror `set_advisor_model`,
  696-708).
- `_resolve_thinking_id()` (session → `"off"`→omit → env default) and
  `_apply_saved_thinking()`, called at build/resume/new/switch (mirror 573/600/616).

**Difference from advisor:** thinking has no tool, so there is no `prepare` hook.
Instead it is applied at the run call site (below).

### 5. Apply to the main loop

`runtime/controller.py` — the `agent.run(...)` call at `controller.py:836-845`
currently passes **no** `model_settings`. Add `model_settings=` built per-turn:

```python
settings = settings_for(self._get_thinking(), _DEFAULT_MODEL_SETTINGS)
```

`_get_thinking()` is a closure sourcing the session's current level (mirror the
existing `get_model` closure). pydantic-ai merges a per-run `model_settings` over
the agent-level default, and the model object identity is untouched (so `/model`
and claude-cli wiring, which key on model identity, are unaffected).

**claude-cli main provider:** marim's `model_settings` do not reach Claude's own
loop, so the thinking level is a no-op there (same class of limitation as the
advisor tool being absent under claude-cli). Documented, not worked around. A
claude-cli level still applies when used as a *sub-agent/aux* model via the
normal pydantic-ai path.

### 6. Apply to sub-agents (full parity)

Resolution precedence **spawn override → spec frontmatter → inherited session
level**, mirroring tiers:

- `workspace/agents.py` — add `AgentDef.thinking: str | None = None` (near `tier`,
  82-84); parse `thinking:` (with `effort:` alias) via `_opt_str` in `_parse_agent`
  and normalize against `THINKING_LEVELS` (mirror tier block 178-181); pass into
  the `AgentDef(...)` construction (182-192).
- `subagents/runner.py` — `SubagentRunner.build(..., thinking=...)` resolves via
  `resolve_thinking(override, defn.thinking, inherited)` and merges the result
  into the per-spawn `ModelSettings` passed to `Agent(...)` at `runner.py:434`
  (currently passes the shared settings verbatim). `inherited` = the live session
  level, supplied to the runner (a `thinking_default` callable at construction,
  read at spawn so mid-session `/think` affects new spawns).
- Thread `thinking` through the spawn chain exactly like `tier`:
  `run`/`run_background` → `_execute_spawn` → `_prepare_spawn` → `build`
  (`runner.py:860,899,587,642,655,698,301`).
- `tools/spawn_tools.py` — add `thinking: str | None = None` to `spawn_agent`
  (218-233), documented in the docstring next to `tier` (~295-301), threaded into
  the foreground `run_subagent(...)` (362-365) and background `_spawn_background`
  (344-356) dispatches; update the `run_subagent`/`run_background_agent` service
  signatures accordingly.
- **UI:** surface the resolved sub-agent level via an `on_subagent_thinking`
  callback paralleling `on_subagent_model` (`runtime/deps.py:208`, fired in
  `_prepare_spawn` at `runner.py:715-729`); the TUI sub-agent card shows it
  (mirror `stream_render.py:961-971` / `SubAgentWidget.set_model`).

### 7. TUI controls (mirror advisor)

`interfaces/tui/commands.py`:
- `_cmd_thinking(app, arg)` (mirror `_cmd_advisor`, 176-188): `off`/level →
  `app.harness.set_thinking_level(...)`; blank → open the picker. No mid-turn
  refusal (applies to the next turn).
- Registry entry `Command("think", "set thinking level: /think [level|off] (picker if blank)", _cmd_thinking, aliases=("effort",))` (list at 510-543).

`interfaces/tui/app.py` / a new modal:
- `open_thinking_picker()` → a small `ThinkingPickerModal` over the fixed
  6-item level list (current highlighted), annotating the current model's support
  (see §8). Simpler than `ModelPickerModal` (no fetch). Callback →
  `set_thinking_level`.
- Startup status line: if the level is non-`off`, append
  `NoticeMessage(f"Thinking: {level} · /think")` (mirror advisor at
  `app.py:221-226`).

`interfaces/tui/settings.py`:
- A "Thinking" `srow` with a value Static + "change" button opening the picker
  (mirror the advisor row at 440-468); persists `MARIM_THINKING` via
  `save_env_settings` (mirror `_on_advisor_chosen`, 842-862); "applies to new
  sessions" status. Value helper mirrors `_advisor_value_text` (595-596).

Autocomplete picks up the new command automatically.

### 8. Detection / annotation (best-effort, never blocks)

Per the chosen "show always, annotate if unsupported" policy — detection only
decorates the picker, never disables a level.

- `workspace/catalog.py` — add `ModelEntry.supports_thinking: bool | None`
  (near `supports_images`, 17-36), parsed from the OpenRouter `/models`
  `supported_parameters` array (`"reasoning"` ∈ it ⇒ `True`) in `parse_models`
  (39-66). Helper `model_supports_thinking(entries, id)` mirroring
  `model_supports_images` (254-260).
- Live fallback (valid, but **deferred from v1**): `provider.model_profile(model_id).get('supports_thinking')`
  — `ModelProfile` is a `TypedDict(total=False)` declaring `supports_thinking: bool`, so `.get(...)` is a real API (marim already calls `provider.model_profile` in `config/openrouter_cost.py:203`). Deferred only to avoid threading a provider handle into the TUI picker; the catalog signal covers the annotate-only requirement. Restore this fallback if a provider without catalog coverage needs annotation.
- **Absence of data ⇒ no annotation** (never cry wolf). Annotate only on a
  definite negative, e.g. "· may be unsupported by <model>".

### 9. Headless

- `MARIM_THINKING` env is honored via bootstrap.
- A `--think <level>` launch flag on the default/headless command (parallel to
  Claude Code's `--effort`), wired through `builder.with_thinking`.

### 10. Docs

- CLAUDE.md subsystem note (a `thinking.py` bullet, mirroring the advisor bullet).
- `.env.example`: `MARIM_THINKING`.
- The `spawn_agent` docstring documents the `thinking` override (it is
  model-facing product copy).

## Data flow

**Set:** `/think high` → `_cmd_thinking` → `harness.set_thinking_level("high")`
→ live value updated + `ctrl.set_thinking("high")` persists to the session store.

**Main turn:** `controller` turn assembly → `settings_for(current_level, base)`
→ `agent.run(..., model_settings={... "thinking": "high"})` → pydantic-ai
translates per provider.

**Spawn:** `spawn_agent(..., thinking=?)` → `resolve_thinking(override,
spec.thinking, inherited_session_level)` → merged into the sub-agent's
`ModelSettings` at `Agent(...)` build → `on_subagent_thinking` reports the
resolved level to the card.

## Error handling / edge cases

- Invalid level (CLI/env/frontmatter/command arg) → normalized to `None` and
  ignored (never raises); `/think garbage` shows a brief notice listing valid
  levels.
- `off`/unset ⇒ no `thinking` key ⇒ identical to current behavior; existing
  sessions without the field load as `None` ⇒ env default ⇒ effectively off.
- Model doesn't support thinking ⇒ pydantic-ai ignores the setting; we annotate
  in the picker but still allow it (best-effort detection).
- claude-cli main provider ⇒ no-op (documented).
- Mid-turn `/think` ⇒ applies to the next turn / next spawn (no rebuild); safe
  because it's a per-run setting, not agent state.

## Testing

Pure helpers (direct unit tests, per the pure-helper convention):
- `parse_thinking_level` (valid/case/invalid → None).
- `settings_for` (off/None → base unchanged; level → adds `thinking`).
- `resolve_thinking` precedence (override > spec > inherited; junk falls through).
- `parse_models` → `supports_thinking` from `supported_parameters`.

Integration:
- Store round-trip (`save`/`save_meta`/`load`) + `latest_thinking` inheritance.
- `harness.set_thinking_level` persists and the controller assembles
  `model_settings.thinking` from the live value.
- Sub-agent: `build` injects the resolved level into `model_settings`;
  `spawn_agent` threads the override through to resolution.
- TUI (light): command parse routes off/level/blank; settings row persists
  `MARIM_THINKING`.

CI order to satisfy: `ruff → pyright → pytest` on 3.10/3.12/3.14; coverage ≥90%.
`requires-python >=3.10` (no 3.11+-only syntax).

## Global constraints

- Level vocabulary is exactly `off, minimal, low, medium, high, xhigh` (one
  ordered constant, reused everywhere).
- `off` omits the `thinking` key — never emits `thinking=False`.
- Sub-agent resolution precedence is exactly: spawn override → spec frontmatter
  → inherited session level.
- Ruff line length 100; cyclomatic complexity ≤10 (extract helpers, no blanket
  `# noqa: C901`).
- Mirror the advisor and tier patterns rather than inventing parallel structures.

## Out of scope (v1)

- Per-tier thinking defaults (no thinking config table; resolution is
  override → spec → inherited only).
- An explicit `thinking=False` hard-disable distinct from `off`.
- Making the thinking level reach the claude-cli **main** provider's own loop.
- Continuous token-budget control (`MAX_THINKING_TOKENS`-style); we set the
  discrete unified `thinking` level only.
