# Thinking Level Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user choose a *thinking level* (reasoning effort) that marim applies to the model via pydantic-ai's `ModelSettings.thinking` — controllable from the TUI (`/think` command + a Settings row), persisted per session, seeded by `MARIM_THINKING`, and flowing to sub-agents with a per-sub-agent override (spec frontmatter and a `spawn_agent` argument).

**Architecture:** A new root module `thinking.py` owns the ordered level vocabulary and three pure helpers: `parse_thinking_level` (env/CLI/command coercion), `settings_for` (fold a level into a `ModelSettings`, or leave it byte-identical when the level is `off`/unset), and `resolve_thinking` (sub-agent precedence: spawn override → spec frontmatter → inherited session level). The main loop applies the level per turn in `TurnController` by merging `settings_for(level, _DEFAULT_MODEL_SETTINGS)` into `agent.run(model_settings=…)`; the session-persisted level rides on `SessionStore.thinking` (mirroring `store.model`), and `Harness` holds the live `thinking_level_id` that both the controller closure and the sub-agent runner read lazily — so a `/think` switch applies to the next turn/spawn with no agent rebuild. Sub-agents merge their resolved level into each spawn's `ModelSettings` at build time. This mirrors the **advisor** feature (session-persisted, TUI-controlled, env-seeded knob) and the **model-tier** mechanism (override → spec → inherited resolution).

**Tech Stack:** Python 3.10+, pydantic-ai 2.8 (`ModelSettings.thinking`, `TestModel`/`FunctionModel` for tests), Textual (TUI), pytest + anyio.

**Spec:** `docs/superpowers/specs/2026-07-22-thinking-level-design.md` (approved).

## Global Constraints

- Levels are exactly `off / minimal / low / medium / high / xhigh` — one ordered `THINKING_LEVELS` tuple is the single source of truth.
- `off` OMITS the `thinking` key entirely (never `thinking=False`): backward-compatible and byte-identical to today's behavior. Existing session files (no `thinking` key) load as `None` and behave exactly as before.
- Sub-agent precedence is EXACTLY: spawn override → spec frontmatter → inherited session level.
- No pydantic-ai bump — 2.8.0 already exposes `ModelSettings.thinking`.
- `requires-python >= 3.10` — no 3.11+-only syntax.
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pytest`/`pip`.
- Async tests use `anyio` (`@pytest.mark.anyio`); pytest-asyncio is NOT installed.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity cap 10 (extract helpers, never a blanket `# noqa: C901`).
- pyright standard mode: 0 errors. Coverage ≥ 90%.
- CI order before claiming done: ruff → pyright → pytest, on Python 3.10 / 3.12 / 3.14.
- Mirror the advisor + model-tier patterns; preserve the codebase's long "why" comments and write new ones in the same style.
- Under the `claude-cli` MAIN provider, thinking is a documented no-op (marim's own `ModelSettings` don't reach Claude Code) — do not attempt to force it.
- Capability detection is best-effort and NEVER blocks a level: it may annotate the UI, but any level is always selectable and always applied.
- No live-model tests: `TestModel`/`FunctionModel` only. (User rule: never run a paid model without explicit approval.)
- Each task: write the failing test first, run it to fail, implement minimally, run to pass, then commit with an EXPLICIT `git add <named files>` — never `git add -A`/`git add .`.

## File Structure

- `src/marim_harness/thinking.py` — **CREATE.** Level vocabulary + pure helpers (`THINKING_LEVELS`, `parse_thinking_level`, `settings_for`, `resolve_thinking`).
- `src/marim_harness/config/model.py` — MODIFY. `ModelConfig.thinking_level` field + `MARIM_THINKING` parse.
- `.env.example` — MODIFY. Document `MARIM_THINKING`.
- `src/marim_harness/session/store.py` — MODIFY. Persist `SessionStore.thinking` (mirror `model`).
- `src/marim_harness/session/ctrl.py` — MODIFY. `saved_thinking_id` property + `set_thinking`.
- `src/marim_harness/runtime/harness.py` — MODIFY. `HarnessConfig.thinking_level`, `Harness.thinking_level_id`, live setter, session-change hooks, `get_thinking` threaded into `build_collaborators`.
- `src/marim_harness/runtime/controller.py` — MODIFY. `get_thinking` param + `_turn_model_settings()` applied on `agent.run`.
- `src/marim_harness/runtime/builder.py` — MODIFY. `with_thinking(level)`.
- `src/marim_harness/runtime/bootstrap.py` — MODIFY. Pass `thinking_level=cfg.thinking_level`.
- `src/marim_harness/runtime/deps.py` — MODIFY. `SubAgentThinkingCb` + `UIHooks.on_subagent_thinking`.
- `src/marim_harness/subagents/runner.py` — MODIFY. `thinking_default` at construction + per-spawn thinking merge + UI report.
- `src/marim_harness/tools/spawn_tools.py` — MODIFY. `thinking` arg on `spawn_agent`, threaded to the service dispatches.
- `src/marim_harness/workspace/agents.py` — MODIFY. `AgentDef.thinking` frontmatter (`thinking:` / `effort:`).
- `src/marim_harness/workspace/catalog.py` — MODIFY. `ModelEntry.supports_thinking` + `model_supports_thinking` helper.
- `src/marim_harness/interfaces/tui/commands.py` — MODIFY. `/think` command (alias `effort`).
- `src/marim_harness/interfaces/tui/app.py` — MODIFY. `open_thinking_picker` + startup status line.
- `src/marim_harness/interfaces/tui/thinking_picker.py` — **CREATE.** Fixed-list level picker modal.
- `src/marim_harness/interfaces/tui/settings.py` — MODIFY. "Thinking" Settings row.
- `src/marim_harness/interfaces/tui/subagents/card.py` — MODIFY. `set_thinking_level` label.
- `src/marim_harness/interfaces/tui/stream_render.py` — MODIFY. `on_subagent_thinking` handler.
- `src/marim_harness/interfaces/cli/default_cmd.py` — MODIFY. `--think <level>` headless flag.
- `CLAUDE.md` — MODIFY. Subsystem note.

---

### Task 1: Thinking core module (`thinking.py`)

**Files:**
- Create: `src/marim_harness/thinking.py`
- Test: `tests/test_thinking.py`

**Interfaces:**
- Consumes: `ModelSettings` from `pydantic_ai.settings`.
- Produces (later tasks rely on these exact names):
  - `THINKING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh")`
  - `parse_thinking_level(value: str | None) -> str | None` (case-insensitive; unknown ⇒ `None`)
  - `settings_for(level: str | None, base: ModelSettings) -> ModelSettings` (`off`/`None` ⇒ `base` unchanged; else `{**base, "thinking": level}`)
  - `resolve_thinking(override: str | None, spec: str | None, inherited: str | None) -> str | None` (precedence override → spec → inherited; unrecognized candidates fall through)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_thinking.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_thinking.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.thinking'`

- [ ] **Step 3: Write the implementation**

Create `src/marim_harness/thinking.py`:

```python
"""Thinking level (reasoning effort): the vocabulary and the pure helpers that
turn a chosen level into a pydantic-ai ``ModelSettings.thinking`` value.

marim exposes one ordered vocabulary — ``off`` plus pydantic-ai's five
``ThinkingEffort`` steps — as a single source of truth for the env parser, the
session store, the TUI picker, and the sub-agent resolver. ``off`` is a
first-class member that means "no reasoning effort": ``settings_for`` OMITS the
key entirely for it (never ``thinking=False``), so an unset or explicitly-off
session is byte-identical to marim's pre-thinking behavior.

Everything here is side-effect-free and unit-tested directly (see
coding-guidelines.md's pure-helper split): ``settings_for`` never mutates its
base, and ``resolve_thinking`` mirrors ``subagents/tiers.resolve_tier`` — an
unrecognized candidate falls through to the next precedence level instead of
erroring, so a fat-fingered override degrades gracefully."""

from __future__ import annotations

from pydantic_ai.settings import ModelSettings

# The ordered thinking vocabulary. ``off`` is FIRST and disables reasoning
# effort (settings_for omits the key). The rest are pydantic-ai's
# ``ThinkingEffort`` steps, ascending. Persisted verbatim into session JSON and
# read by the config parser + TUI — changing a spelling orphans saved sessions.
THINKING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh")


def parse_thinking_level(value: str | None) -> str | None:
    """Coerce a raw string (env var, CLI flag, /think arg) to a canonical level,
    or ``None`` when it is blank or unrecognized. Case-insensitive and
    whitespace-tolerant. ``None`` means "unset" — the caller then falls back to
    its own default (the env default, or the inherited session level)."""
    if value is None:
        return None
    candidate = value.strip().lower()
    return candidate if candidate in THINKING_LEVELS else None


def settings_for(level: str | None, base: ModelSettings) -> ModelSettings:
    """Fold ``level`` into a copy of ``base`` as ``ModelSettings.thinking``.

    ``off`` and ``None`` return ``base`` unchanged (the key is OMITTED, never
    set to ``False``) — that is what keeps an unset/disabled session
    byte-identical to today's per-run settings and preserves prompt caching.
    Any other level returns a NEW mapping with ``thinking`` set; ``base`` is
    never mutated (a per-round settings object must not accumulate state)."""
    if not level or level == "off":
        return base
    return ModelSettings({**base, "thinking": level})


def resolve_thinking(
    override: str | None, spec: str | None, inherited: str | None
) -> str | None:
    """Resolve a sub-agent's thinking level by precedence: the spawn-call
    ``override`` first, then the spec's ``thinking:`` frontmatter, then the
    ``inherited`` session level. Returns the first candidate that is a known
    level (``off`` counts — an explicit off wins), else ``None``.

    An unrecognized candidate (a raw model slug in the override slot, a typo'd
    label) falls through to the next level rather than erroring — the same
    graceful-degrade contract as ``subagents.tiers.resolve_tier``."""
    for candidate in (override, spec, inherited):
        if candidate in THINKING_LEVELS:
            return candidate
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_thinking.py -v`
Expected: 8 passed.

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src/marim_harness/thinking.py tests/test_thinking.py && uv run pyright`
Expected: clean.

```bash
git add src/marim_harness/thinking.py tests/test_thinking.py
git commit -m "feat(thinking): core vocabulary and pure helpers (parse/settings_for/resolve)"
```

---

### Task 2: Env config (`MARIM_THINKING` → `ModelConfig`)

**Files:**
- Modify: `src/marim_harness/config/model.py` (`ModelConfig` fields near the `advisor_*` block, line ~189; `_common_kwargs()` return near line ~301; import at top)
- Modify: `.env.example` (after the Advisor block, line ~154)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Consumes: `parse_thinking_level` (Task 1).
- Produces: `ModelConfig.thinking_level: str | None` (default `None`), read from `MARIM_THINKING`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_thinking_env_config(monkeypatch):
    monkeypatch.setenv("MARIM_THINKING", "high")
    cfg = load_config()
    assert cfg.thinking_level == "high"


def test_thinking_env_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("MARIM_THINKING", "  Medium ")
    cfg = load_config()
    assert cfg.thinking_level == "medium"


def test_thinking_env_default_is_none(monkeypatch):
    monkeypatch.delenv("MARIM_THINKING", raising=False)
    cfg = load_config()
    assert cfg.thinking_level is None


def test_thinking_env_unknown_value_is_none(monkeypatch):
    monkeypatch.setenv("MARIM_THINKING", "ultra")
    cfg = load_config()
    assert cfg.thinking_level is None
```

(`load_config` is already imported at the top of `tests/test_config.py`; if not, add `from marim_harness.config import load_config`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_config.py -k thinking -v`
Expected: FAIL with `TypeError` (unexpected field) or `AttributeError: 'ModelConfig' object has no attribute 'thinking_level'`

- [ ] **Step 3: Implement**

In `src/marim_harness/config/model.py`, add the import near the other package-root imports at the top of the file:

```python
from ..thinking import parse_thinking_level
```

Add the field to `ModelConfig` directly after the `advisor_max_uses` field (line ~196):

```python
    # Thinking level (reasoning effort) applied to the model via
    # ModelSettings.thinking. One of thinking.THINKING_LEVELS, or None (unset —
    # no reasoning effort, marim's pre-thinking behavior). The session store's
    # ``thinking`` overrides this at runtime (see Harness._resolve_thinking_id).
    thinking_level: str | None = None
```

In `_common_kwargs()`'s returned dict, after the `advisor_max_uses=...` line (line ~305):

```python
        # parse_thinking_level folds an unknown/blank MARIM_THINKING to None
        # (unset), so a typo silently disables thinking rather than crashing
        # startup — detection/selection is always best-effort (see the spec).
        thinking_level=parse_thinking_level(os.getenv("MARIM_THINKING")),
```

In `.env.example`, after the Advisor block (line ~154):

```
# --- Thinking level (reasoning effort) ---
# Reasoning effort applied to the model via ModelSettings.thinking. One of:
# off, minimal, low, medium, high, xhigh. Unset or 'off' = no reasoning effort
# (marim's default). /think overrides it per session, live.
# MARIM_THINKING=medium
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_config.py -v`
Expected: all pass (including pre-existing tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/config/model.py .env.example tests/test_config.py
git commit -m "feat(thinking): MARIM_THINKING env config"
```

---

### Task 3: Session persistence (`SessionStore.thinking`)

**Files:**
- Modify: `src/marim_harness/session/store.py` (`SessionInfo` ~line 152; `SessionStore.__init__` ~line 160; `save` payload ~line 186; `save_meta` ~line 248; `list()` `SessionInfo(...)` ~line 360; `store()` ~line 380; `create()` ~line 418; add `latest_thinking`)
- Modify: `src/marim_harness/session/ctrl.py` (after `set_advisor`, ~line 346)
- Test: `tests/test_thinking_session.py`

**Interfaces:**
- Produces: `SessionStore.thinking: str | None`; `SessionInfo.thinking: str | None`; `SessionManager.latest_thinking() -> str | None`; `SessionController.saved_thinking_id` property; `SessionController.set_thinking(value: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_thinking_session.py`:

```python
"""Session persistence of the thinking level, mirroring the ``model`` field:
save/save_meta/store() round-trip, create()-inherits-latest, and old files
(no key) loading as None."""

from pydantic_ai.usage import RunUsage

from marim_harness.session import SessionManager


def test_thinking_round_trips_through_save(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.thinking = "high"
    store.save([], RunUsage())
    reopened = SessionManager(tmp_path).store(store.session_id)
    assert reopened.thinking == "high"


def test_save_meta_patches_thinking_without_touching_messages(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.save([], RunUsage())
    store.thinking = "off"
    store.save_meta()
    reopened = SessionManager(tmp_path).store(store.session_id)
    assert reopened.thinking == "off"


def test_create_inherits_latest_thinking(tmp_path):
    manager = SessionManager(tmp_path)
    first = manager.create()
    first.thinking = "medium"
    first.save([], RunUsage())
    fresh = manager.create()
    assert fresh.thinking == "medium"


def test_create_without_history_has_no_thinking(tmp_path):
    manager = SessionManager(tmp_path)
    assert manager.create().thinking is None


def test_old_session_files_load_with_none(tmp_path):
    # A pre-thinking session file simply has no key → None (behaves as before).
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.save([], RunUsage())
    import json
    data = json.loads(store.path.read_text())
    data.pop("thinking", None)
    store.path.write_text(json.dumps(data))
    reopened = SessionManager(tmp_path).store(store.session_id)
    assert reopened.thinking is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_thinking_session.py -v`
Expected: FAIL with `AttributeError: 'SessionStore' object has no attribute 'thinking'`

- [ ] **Step 3: Implement — store.py**

`SessionInfo` (line ~152): add field after `advisor_model`:

```python
    advisor_model: str | None = None
    thinking: str | None = None
```

`SessionStore.__init__` (line ~160): add parameter and assignment (after the existing `advisor_model` param/assignment):

```python
    def __init__(self, path, workspace_root, session_id: str, name: str,
                 auto_named: bool = False, model: str | None = None,
                 advisor_model: str | None = None,
                 thinking: str | None = None) -> None:
        ...
        self.advisor_model = advisor_model
        # The thinking level this session chose (one of thinking.THINKING_LEVELS
        # — including "off" as an explicit disable — or None: unset, inherit
        # MARIM_THINKING).
        self.thinking = thinking
```

`save` payload (line ~186): after `"advisor_model": self.advisor_model,` add:

```python
            "thinking": self.thinking,
```

`save_meta` (line ~248): after `data["advisor_model"] = self.advisor_model` add:

```python
            data["thinking"] = self.thinking
```

`list()` `SessionInfo(...)` construction (line ~360): after `advisor_model=data.get("advisor_model"),` add:

```python
                    thinking=data.get("thinking"),
```

`store()` (line ~380): after `advisor_model = meta.get("advisor_model")` add `thinking = meta.get("thinking")`, and pass it through the `SessionStore(...)` return:

```python
        return SessionStore(
            path, self.workspace_root, session_id, name,
            auto_named=auto_named, model=model, advisor_model=advisor_model,
            thinking=thinking,
        )
```

`create()` (line ~418): after the advisor-inherit block add:

```python
        # Same inheritance as the model/advisor above: a new session keeps the
        # thinking level the user last chose (including an explicit "off").
        if store.thinking is None:
            store.thinking = self.latest_thinking()
        return store
```

After `latest_advisor_model()` (line ~445) add:

```python
    def latest_thinking(self) -> str | None:
        """The thinking level of the most recent session, or *None*."""
        latest = self.latest()
        return latest.thinking if latest is not None else None
```

- [ ] **Step 4: Implement — ctrl.py**

In `src/marim_harness/session/ctrl.py`, after `set_advisor` (line ~346):

```python
    @property
    def saved_thinking_id(self) -> str | None:
        """The thinking level persisted with this session — a level name
        (including "off"), or None (unset) — or None if no store."""
        return self.store.thinking if self.store is not None else None

    def set_thinking(self, value: str) -> None:
        """Persist the session's thinking choice (a member of
        thinking.THINKING_LEVELS). Same metadata-only patch rules as
        ``set_advisor``: a switch can land mid-turn when in-memory history must
        never reach disk, so patch the header when a file exists, else force one
        clean persist."""
        if self.store is not None:
            self.store.thinking = value
            if self.store.path.exists():
                self.store.save_meta()
            else:
                self.persist(force=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_thinking_session.py tests/test_session.py tests/test_agent_sessions.py -q`
Expected: all pass.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/session/store.py src/marim_harness/session/ctrl.py tests/test_thinking_session.py
git commit -m "feat(thinking): persist the session thinking level"
```

---

### Task 4: Harness live level + main-loop application (`TurnController`)

**Files:**
- Modify: `src/marim_harness/runtime/harness.py` (`HarnessConfig` after `advisor_max_uses`, ~line 213; `Harness.__init__` — set `thinking_level_id` early ~line 491, thread `get_thinking` into `build_collaborators` ~line 499 and `TurnController` ~line 526, apply-saved ~line 543; new methods after `set_advisor_model` ~line 748; session-change hooks in `resume`/`new_session`/`switch_session`)
- Modify: `src/marim_harness/runtime/controller.py` (import `settings_for` at top; `TurnController.__init__` ~line 269 add `get_thinking`; `_turn_model_settings()` helper after `_turn_model` ~line 330; `agent.run(...)` ~line 870 add `model_settings=`)
- Test: `tests/test_thinking_controller.py`

**Interfaces:**
- Consumes: `settings_for` (Task 1); `_DEFAULT_MODEL_SETTINGS` (harness.py); `SessionController.saved_thinking_id`/`set_thinking` (Task 3); `ModelConfig.thinking_level` (Task 2).
- Produces: `HarnessConfig.thinking_level`; `Harness.thinking_level_id: str | None`; `Harness.set_thinking_level(level: str, *, persist: bool = True)`; `Harness.get_thinking() -> str | None`; `TurnController.get_thinking` param + `TurnController._turn_model_settings() -> ModelSettings`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_thinking_controller.py`:

```python
"""Harness thinking wiring + main-loop application: the config default seeds
the live level, the session level overrides it, the live setter switches and
persists, and TurnController folds the level into per-run model_settings."""

import pytest
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.harness import Harness, _DEFAULT_MODEL_SETTINGS
from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionManager
from marim_harness.tools.provider import BuiltinToolProvider


def _harness(tmp_path, **kwargs) -> Harness:
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    return Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, "Be helpful.", **kwargs
    )


def test_config_default_seeds_the_live_level(tmp_path):
    h = _harness(tmp_path, thinking_level="high")
    assert h.thinking_level_id == "high"
    assert h.get_thinking() == "high"


def test_unconfigured_leaves_the_level_none(tmp_path):
    h = _harness(tmp_path)
    assert h.thinking_level_id is None


def test_session_level_overrides_config_default(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.thinking = "low"
    h = _harness(tmp_path, store=store, manager=manager, thinking_level="high")
    assert h.thinking_level_id == "low"


def test_session_off_overrides_config_default(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.thinking = "off"
    h = _harness(tmp_path, store=store, manager=manager, thinking_level="high")
    assert h.thinking_level_id == "off"


def test_set_thinking_level_switches_and_persists(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(tmp_path, store=store, manager=manager)
    h.set_thinking_level("medium")
    assert h.thinking_level_id == "medium"
    assert store.thinking == "medium"


def test_controller_folds_level_into_run_settings(tmp_path):
    h = _harness(tmp_path, thinking_level="high")
    settings = h.controller._turn_model_settings()
    assert settings["thinking"] == "high"
    assert settings["parallel_tool_calls"] is True


def test_controller_off_leaves_settings_identical_to_default(tmp_path):
    h = _harness(tmp_path)  # unset → None
    assert h.controller._turn_model_settings() == _DEFAULT_MODEL_SETTINGS
    h.set_thinking_level("off")
    assert h.controller._turn_model_settings() == _DEFAULT_MODEL_SETTINGS
    assert "thinking" not in h.controller._turn_model_settings()


@pytest.mark.anyio
async def test_run_applies_settings_and_does_not_mutate_default(tmp_path):
    h = _harness(tmp_path, thinking_level="high")
    await h.run_turn("hi")
    # _DEFAULT_MODEL_SETTINGS is the shared agent-level default; settings_for
    # must copy, never mutate it.
    assert "thinking" not in _DEFAULT_MODEL_SETTINGS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_thinking_controller.py -v`
Expected: FAIL with `TypeError` (unknown `HarnessConfig` field `thinking_level`) / `AttributeError: 'Harness' object has no attribute 'thinking_level_id'`.

- [ ] **Step 3: Implement — controller.py**

In `src/marim_harness/runtime/controller.py`, add the import near the top with the other package imports:

```python
from ..thinking import settings_for
```

In `TurnController.__init__` (line 269), add a `get_thinking` parameter after `get_model` (line 277) and store it (after `self.get_model = get_model`, line 286):

```python
    def __init__(
        self,
        agent: HarnessAgent,
        session: SessionController,
        checkpoints: CheckpointManager,
        hooks: TurnHooks,
        mcp: McpManager,
        deps: Deps,
        get_model: Callable[[], Model],
        get_thinking: Callable[[], str | None],
        lsp_toolset: FunctionToolset[Deps] | None = None,
    ) -> None:
        self.agent = agent
        ...
        self.get_model = get_model
        # Live getter for the session thinking level (closes over the Harness's
        # mutable thinking_level_id), read PER ROUND so a /think switch applies
        # to the next turn with no agent rebuild — the get_model pattern.
        self.get_thinking = get_thinking
        self.lsp_toolset = lsp_toolset
```

Add `_turn_model_settings` directly after `_turn_model` (line ~330):

```python
    def _turn_model_settings(self) -> ModelSettings:
        """Per-round model settings: the agent-level default with the live
        thinking level folded in (settings_for OMITS the key for off/unset, so
        an unset session stays byte-identical to the agent default and keeps the
        prompt cache warm). Deferred import of the harness default keeps the
        harness→controller import edge one-directional (harness imports
        controller at module top)."""
        from .harness import _DEFAULT_MODEL_SETTINGS

        return settings_for(self.get_thinking(), _DEFAULT_MODEL_SETTINGS)
```

Add a top-of-module import for the type (near the other pydantic-ai imports):

```python
from pydantic_ai.settings import ModelSettings
```

In the `agent.run(...)` call (line ~870), add the `model_settings` argument:

```python
                    result = await self.agent.run(
                        user_prompt,
                        model=self._turn_model(),
                        model_settings=self._turn_model_settings(),
                        message_history=self.session.history,
                        deps=self.deps,
                        deferred_tool_results=deferred_results,
                        event_stream_handler=event_stream_handler,
                        toolsets=toolsets,
                        usage=round_usage,
                    )
```

- [ ] **Step 4: Implement — HarnessConfig + Harness**

In `src/marim_harness/runtime/harness.py`:

`HarnessConfig`: after `advisor_max_uses` (line ~213) add:

```python
    # Thinking level (reasoning effort) applied to the main model per turn via
    # ModelSettings.thinking. One of thinking.THINKING_LEVELS, or None (unset).
    # The session store's ``thinking`` overrides this — see
    # Harness._apply_saved_thinking. Sub-agents inherit the live level via the
    # runner's thinking_default closure (get_thinking below).
    thinking_level: str | None = None
```

In `Harness.__init__`, set the live attribute EARLY — before the `build_collaborators` call (line ~499), so the `get_thinking` closure can capture it:

```python
        # Live thinking level (reasoning effort). Set before build_collaborators
        # and TurnController so their get_thinking closures capture a live
        # attribute; the real value is resolved by _apply_saved_thinking below.
        self._thinking_env_default = cfg.thinking_level
        self.thinking_level_id: str | None = None
```

Thread `get_thinking` into the `build_collaborators` call (line ~499):

```python
        collaborators = build_collaborators(
            model, provider, deps, instructions, cfg,
            get_model=lambda: self.current_model,
            get_thinking=lambda: self.thinking_level_id,
        )
```

Thread `get_thinking` into the `TurnController(...)` construction (line ~518):

```python
        self.controller = TurnController(
            self.agent,
            self.session,
            collaborators.checkpoints,
            collaborators.hooks,
            collaborators.mcp,
            self.deps,
            get_model=lambda: self.current_model,
            get_thinking=lambda: self.thinking_level_id,
            lsp_toolset=collaborators.lsp_toolset,
        )
```

After the advisor block (line ~543), apply the saved level:

```python
        self._apply_saved_thinking()
```

New methods, placed directly after `set_advisor_model` (line ~748):

```python
    def get_thinking(self) -> str | None:
        """The live thinking level (session override → env/config default →
        None). Read by the controller per round and the sub-agent runner per
        spawn, so a switch applies without a rebuild."""
        return self.thinking_level_id

    def _resolve_thinking_id(self) -> str | None:
        """Session override → env/config default → None. A persisted level
        (including "off") beats the env default; None means "unset — inherit
        the env default"."""
        saved = self.session.saved_thinking_id
        return saved if saved is not None else self._thinking_env_default

    def _apply_saved_thinking(self) -> None:
        """Point the live thinking level at the active session's choice. Called
        at build and after every session change (resume/new/switch), mirroring
        ``_apply_saved_model``. No seam to flip: the controller and runner read
        thinking_level_id lazily per round/spawn."""
        self.thinking_level_id = self._resolve_thinking_id()

    def set_thinking_level(self, level: str, *, persist: bool = True) -> None:
        """Switch the thinking level at runtime (a member of
        thinking.THINKING_LEVELS, "off" to disable). Safe mid-turn: the level is
        read per round, so a switch simply applies to the next turn/spawn."""
        self.thinking_level_id = level
        if persist:
            self.session.set_thinking(level)
```

Session-change hooks — add `self._apply_saved_thinking()` in three places (after each existing `self._apply_saved_advisor()` call):

- `resume()` (line ~614)
- `new_session()` (line ~640)
- `switch_session()` (line ~657)

- [ ] **Step 5: Implement — build_collaborators signature + runner default**

In `src/marim_harness/runtime/harness.py`, `build_collaborators` (line 281): add a keyword-only `get_thinking` parameter after `get_model` (line 288):

```python
def build_collaborators(
    model: Model,
    provider: ToolProvider,
    deps: Deps,
    instructions: str,
    cfg: HarnessConfig,
    *,
    get_model: Callable[[], Model],
    get_thinking: Callable[[], str | None] = lambda: None,
) -> Collaborators:
```

Extend its docstring after the `get_model` paragraph:

```python
    ``get_thinking`` is the same kind of live getter (closing over the live
    ``Harness.thinking_level_id``) so a spawned sub-agent inherits the session's
    current thinking level when its own override/spec don't set one. It defaults
    to "no inherited level" for the embedding builder path.
```

In the `SubagentRunner(...)` construction (line ~397), pass the getter after `tiers=cfg.subagent_tiers,` (line 409):

```python
        tiers=cfg.subagent_tiers,
        # Inherited thinking level for spawns whose override/spec don't set
        # one — read lazily per spawn so a /think switch reaches later spawns.
        thinking_default=get_thinking,
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_thinking_controller.py tests/test_agent.py -q`
Expected: all pass. (`test_agent.py` guards the harness/controller construction — the new params must not break it.)

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/harness.py src/marim_harness/runtime/controller.py \
    tests/test_thinking_controller.py
git commit -m "feat(thinking): live level on Harness + main-loop application in TurnController"
```

---

### Task 5: Bootstrap + builder front door

**Files:**
- Modify: `src/marim_harness/runtime/builder.py` (new `with_thinking` after `with_advisor`, line ~183)
- Modify: `src/marim_harness/runtime/bootstrap.py` (`.with_config_overrides(...)`, after `advisor_max_uses=...`, line ~201)
- Modify: `docs/embedding.md` (document `with_thinking`)
- Test: `tests/test_thinking_wiring.py`

**Interfaces:**
- Consumes: `HarnessConfig.thinking_level`, `Harness.thinking_level_id` (Task 4); `ModelConfig.thinking_level` (Task 2).
- Produces: `HarnessBuilder.with_thinking(level: str)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_thinking_wiring.py`:

```python
"""Builder + bootstrap thinking wiring: the builder front door and the
bootstrap env pass-through both land on Harness.thinking_level_id."""

from pydantic_ai.models.test import TestModel


def test_builder_with_thinking(tmp_path):
    from marim_harness.runtime.builder import HarnessBuilder

    h = (
        HarnessBuilder(workspace=tmp_path, model=TestModel(call_tools=[]))
        .with_thinking("high")
        .build()
    )
    assert h.thinking_level_id == "high"


def test_bootstrap_passes_thinking_env(monkeypatch, tmp_path):
    from marim_harness.runtime.bootstrap import build_harness

    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_THINKING", "medium")
    harness = build_harness(tmp_path)
    assert harness.thinking_level_id == "medium"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_thinking_wiring.py -v`
Expected: FAIL with `AttributeError: 'HarnessBuilder' object has no attribute 'with_thinking'`.

- [ ] **Step 3: Implement — builder + bootstrap**

`src/marim_harness/runtime/builder.py`, after `with_advisor` (line ~183):

```python
    def with_thinking(self, level: str) -> HarnessBuilder:
        """Set the thinking level (reasoning effort) applied to the model via
        ModelSettings.thinking. ``level`` is one of thinking.THINKING_LEVELS
        (``off`` disables it — the default). The session store's thinking level
        overrides this at runtime (harness.set_thinking_level switches it live)."""
        return self.with_config_overrides(thinking_level=level)
```

`src/marim_harness/runtime/bootstrap.py`, inside `.with_config_overrides(...)` after the advisor pass-through (line ~201):

```python
            thinking_level=cfg.thinking_level,
```

`docs/embedding.md`: in the with_* composition section, add (adapt heading level to the surrounding doc):

```markdown
### with_thinking(level)

Sets the thinking level (reasoning effort) applied to the main model each turn
via `ModelSettings.thinking`. `level` is one of `off`, `minimal`, `low`,
`medium`, `high`, `xhigh` (`off` omits the setting — the default). The level
persists per session and can be switched live with
`harness.set_thinking_level(...)`; sub-agents inherit it unless their spec or
the spawn call overrides it. Providers that don't support reasoning effort
ignore the setting.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_thinking_wiring.py tests/test_builder.py tests/test_bootstrap.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/builder.py src/marim_harness/runtime/bootstrap.py \
    docs/embedding.md tests/test_thinking_wiring.py
git commit -m "feat(thinking): builder with_thinking + bootstrap env pass-through"
```

---

### Task 6: Sub-agent frontmatter, resolution, and runner injection

**Files:**
- Modify: `src/marim_harness/workspace/agents.py` (`AgentDef` after `tier`, ~line 84; `_parse_agent` after the tier parse/normalize, ~line 181; import at top)
- Modify: `src/marim_harness/subagents/runner.py` (`__init__` after `tiers`, ~line 136/194; `build` — new `thinking` param ~line 307, per-spawn settings merge at the `Agent(...)` `model_settings=` line ~440; `_execute_spawn` ~line 589; `_prepare_spawn` ~line 655; `run` ~line 954; `run_background` ~line 993; import at top)
- Test: `tests/test_thinking_subagent.py`

**Interfaces:**
- Consumes: `resolve_thinking`, `settings_for` (Task 1); `parse_thinking_level` (Task 1); `_DEFAULT_MODEL_SETTINGS` (indirectly, via the runner's `model_settings`).
- Produces: `AgentDef.thinking: str | None`; `SubagentRunner(thinking_default=…)`; `SubagentRunner.build(..., thinking=…)`; a per-spawn `ModelSettings` that folds in `resolve_thinking(override, spec, inherited)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_thinking_subagent.py`:

```python
"""Sub-agent thinking: frontmatter parsing (thinking:/effort:) and the runner's
per-spawn resolution (override → spec → inherited session level), asserted on
the built Agent's model_settings."""

from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.subagents.runner import SubagentRunner
from marim_harness.tools.provider import BuiltinToolProvider
from marim_harness.workspace.agents import AgentDef, _parse_agent


def test_frontmatter_thinking_field_parses():
    defn = _parse_agent(
        "coder",
        "---\nname: coder\nthinking: high\ntools: read_file\n---\nBe careful.",
    )
    assert defn.thinking == "high"


def test_frontmatter_effort_alias_parses_and_normalizes():
    defn = _parse_agent(
        "coder",
        "---\nname: coder\neffort: MEDIUM\ntools: read_file\n---\nGo.",
    )
    assert defn.thinking == "medium"


def test_frontmatter_unknown_thinking_is_dropped():
    defn = _parse_agent(
        "coder",
        "---\nname: coder\nthinking: ultra\ntools: read_file\n---\nGo.",
    )
    assert defn.thinking is None


def _runner(tmp_path, **kwargs) -> SubagentRunner:
    from unittest.mock import MagicMock

    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    return SubagentRunner(
        BuiltinToolProvider(), MagicMock(), deps, MagicMock(), MagicMock(),
        get_model=lambda: TestModel(call_tools=[]),
        model_settings=ModelSettings(parallel_tool_calls=True),
        **kwargs,
    )


def _spec(thinking: str | None) -> AgentDef:
    return AgentDef(
        name="coder", description="", instructions="Go.",
        tools=frozenset({"read_file"}), thinking=thinking,
    )


def test_spawn_override_beats_spec_and_inherited(tmp_path):
    runner = _runner(tmp_path, thinking_default=lambda: "low")
    sub, err = runner.build("coder", defn=_spec("medium"), thinking="high")
    assert err is None
    assert sub.model_settings["thinking"] == "high"


def test_spec_beats_inherited_when_no_override(tmp_path):
    runner = _runner(tmp_path, thinking_default=lambda: "low")
    sub, err = runner.build("coder", defn=_spec("medium"))
    assert sub.model_settings["thinking"] == "medium"


def test_inherited_session_level_when_no_override_or_spec(tmp_path):
    runner = _runner(tmp_path, thinking_default=lambda: "low")
    sub, err = runner.build("coder", defn=_spec(None))
    assert sub.model_settings["thinking"] == "low"


def test_off_resolution_leaves_base_settings_unchanged(tmp_path):
    runner = _runner(tmp_path, thinking_default=lambda: "high")
    sub, err = runner.build("coder", defn=_spec(None), thinking="off")
    assert "thinking" not in sub.model_settings


def test_no_thinking_anywhere_leaves_base_settings_unchanged(tmp_path):
    runner = _runner(tmp_path)  # no thinking_default
    sub, err = runner.build("coder", defn=_spec(None))
    assert "thinking" not in sub.model_settings
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_thinking_subagent.py -v`
Expected: FAIL — `_parse_agent` has no `thinking`; `SubagentRunner.__init__` rejects `thinking_default`; `build` rejects `thinking`.

- [ ] **Step 3: Implement — agents.py**

In `src/marim_harness/workspace/agents.py`, add the import near the top with the other package imports:

```python
from ..thinking import parse_thinking_level
```

`AgentDef` (line ~60): add a field after `tier` (line ~84):

```python
    tier: str | None = None
    # Thinking level (reasoning effort) for this sub-agent: a member of
    # thinking.THINKING_LEVELS, or None (inherit the spawner's level). Set via
    # the `thinking:` (or `effort:`) frontmatter key.
    thinking: str | None = None
```

`_parse_agent` (line ~142): after the tier parse/normalize block (line ~181), add:

```python
    # thinking:/effort: → a canonical level (or None if unknown/absent). Same
    # graceful-drop as tier: a bad value degrades to "inherit", never errors.
    thinking = parse_thinking_level(_opt_str(meta, "thinking") or _opt_str(meta, "effort"))
```

Thread it into the `AgentDef(...)` construction at the end of `_parse_agent` (after `tier=tier,`):

```python
        thinking=thinking,
```

- [ ] **Step 4: Implement — runner.py**

In `src/marim_harness/subagents/runner.py`, add the import near the top with the other package imports (alongside `from .tiers import resolve_tier`, line ~59):

```python
from ..thinking import resolve_thinking, settings_for
```

`SubagentRunner.__init__` (line 126): add a keyword param after `tiers` (line ~136) and store it (after `self._tiers = tiers or SubagentTiers()`, line ~194):

```python
    def __init__(self, provider: ToolProvider, mcp: McpManager, deps: Deps,
                 ...
                 tiers: SubagentTiers | None = None,
                 thinking_default: Callable[[], str | None] | None = None,
                 ...):
        ...
        self._tiers = tiers or SubagentTiers()
        # The inherited session thinking level, read LAZILY per spawn (closes
        # over the live Harness.thinking_level_id) so a /think switch reaches
        # later spawns. None ⇒ nothing to inherit (spec/override still apply).
        self._thinking_default = thinking_default
```

Add a helper method (near `_resolve_spawn_model_id`'s consumers, e.g. right before `build`, line ~302):

```python
    def _spawn_thinking_settings(self, override: str | None, defn) -> ModelSettings | None:
        """The per-spawn model settings: the runner's base settings with the
        resolved thinking level folded in. Precedence: spawn override → spec
        ``thinking:`` → inherited session level. off/None returns the base
        UNCHANGED (settings_for omits the key), so a spawn with no thinking
        anywhere is byte-identical to today's behavior."""
        inherited = self._thinking_default() if self._thinking_default is not None else None
        level = resolve_thinking(override, defn.thinking, inherited)
        if not level or level == "off":
            return self._model_settings
        # base may be None for an embedder that composed no default settings;
        # settings_for needs a concrete mapping to copy.
        return settings_for(level, self._model_settings or ModelSettings())
```

`build` (line 302): add a keyword param after `tier` (line 307):

```python
    def build(
        self, type: str, max_output_chars: int | None = None,
        model: str | None = None, workspace_root=None, *, defn=None,
        depth: int = 0, mask_trigger: int | None = None,
        checkpoint: Callable[[list], None] | None = None,
        output_schema: dict | None = None, tier: str | None = None,
        thinking: str | None = None,
    ) -> tuple[SubAgent | None, str | None]:
```

Extend the docstring (after the ``tier`` paragraph, before "Returns"):

```python
        ``thinking`` is the spawn-call thinking override; it wins over the
        spec's ``thinking:`` frontmatter and the inherited session level
        (resolved in ``_spawn_thinking_settings``). ``None`` lets that
        resolution run; a resolved ``off``/none leaves the base settings intact.
```

At the `Agent(...)` construction, change `model_settings=self._model_settings` (line 440) to the resolved per-spawn settings. Compute it just before the `sub = Agent(...)` call (after `scratchpad_writable = ...`, line ~414):

```python
        sub_settings = self._spawn_thinking_settings(thinking, defn)
```

```python
            model_settings=sub_settings,
```

`_execute_spawn` (line 589): add `thinking: str | None = None` after `tier` (line ~593) and thread it into the `_prepare_spawn`/`build` call it makes (line ~647):

```python
    async def _execute_spawn(
        self, ..., tier: str | None = None, thinking: str | None = None,
    ):
        ...
        prep = await self._prepare_spawn(
            ..., tier=tier, thinking=thinking,
        )
```

`_prepare_spawn` (line 655): add `thinking: str | None = None` after `tier` (line 661) and pass it to `self.build(...)` (line ~701):

```python
    async def _prepare_spawn(
        self, ..., tier: str | None = None, thinking: str | None = None,
    ) -> _SpawnPrep | str:
        ...
        sub, err = self.build(type, max_output_chars, model, work_root, defn=defn,
                              depth=depth, mask_trigger=mask_trigger,
                              checkpoint=checkpoint, output_schema=output_schema,
                              tier=tier, thinking=thinking)
```

`run` (line 954) and `run_background` (line 993): add `thinking: str | None = None` after `tier` in each signature and thread it into the `_execute_spawn(...)` call each makes.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_thinking_subagent.py tests/test_subagents.py tests/test_agents.py -q`
Expected: all pass.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/workspace/agents.py src/marim_harness/subagents/runner.py \
    tests/test_thinking_subagent.py
git commit -m "feat(thinking): sub-agent frontmatter, per-spawn resolution, runner injection"
```

---

### Task 7: `spawn_agent` thinking override + service dispatch

**Files:**
- Modify: `src/marim_harness/tools/spawn_tools.py` (`_spawn_background` ~line 151; `spawn_agent` signature after `tier` ~line 231; docstring ~line 295; `run_subagent` dispatch ~line 357/362)
- Modify: `src/marim_harness/runtime/deps.py` (the `SubAgentRunner`/`BackgroundAgentRunner` type aliases, ~line 54)
- Test: `tests/test_thinking_spawn_tool.py`

**Interfaces:**
- Consumes: `SubagentRunner.run(..., thinking=…)` / `run_background(..., thinking=…)` (Task 6).
- Produces: `spawn_agent(..., thinking: str | None = None)`; the `run_subagent`/`run_subagent_background` service seams carry a trailing `thinking` argument.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_thinking_spawn_tool.py`:

```python
"""spawn_agent's thinking override reaches the run_subagent service seam."""

from types import SimpleNamespace

import pytest

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.tools import spawn_tools


def _ctx(tmp_path, run_subagent):
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    deps.services.run_subagent = run_subagent
    return SimpleNamespace(deps=deps, tool_call_id="tc1")


@pytest.mark.anyio
async def test_spawn_forwards_thinking_to_service(tmp_path):
    seen = {}

    async def run_subagent(*args):
        seen["args"] = args
        return "done"

    ctx = _ctx(tmp_path, run_subagent)
    out = await spawn_tools.spawn_agent(
        ctx, "coder", "do it", thinking="high"
    )
    assert out == "done"
    # thinking rides at the tail of the positional dispatch (mirrors tier).
    assert "high" in seen["args"]


@pytest.mark.anyio
async def test_spawn_defaults_thinking_none(tmp_path):
    seen = {}

    async def run_subagent(*args):
        seen["args"] = args
        return "done"

    ctx = _ctx(tmp_path, run_subagent)
    await spawn_tools.spawn_agent(ctx, "coder", "do it")
    assert None in seen["args"]
```

(If `spawn_agent`'s existing signature requires extra positional args in your build, adapt the call to match — the assertion of interest is that `thinking` reaches the seam.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_thinking_spawn_tool.py -v`
Expected: FAIL — `spawn_agent()` got an unexpected keyword argument `thinking`.

- [ ] **Step 3: Implement — deps.py seam types**

In `src/marim_harness/runtime/deps.py`, extend the `SubAgentRunner` and `BackgroundAgentRunner` `Callable[...]` aliases (line ~54) to carry a trailing `str | None` (the thinking level) after the existing `tier` argument. Add a comment on the new arg:

```python
# (type, task, mcp_names, max_output_chars, model, isolation, stream_id,
#  caller_depth, tier, thinking) -> the sub-agent's final report. ``thinking``
#  is the spawn-call reasoning-effort override (None ⇒ inherit spec/session).
SubAgentRunner = Callable[
    [str, str, list[str] | None, int | None, str | None, object, str, int,
     str | None, str | None],
    Awaitable[str],
]
```

(Apply the same trailing `str | None` addition to the `BackgroundAgentRunner` alias if it is separate.)

- [ ] **Step 4: Implement — spawn_tools.py**

`spawn_agent` (line 218): add `thinking: str | None = None` after `tier` (line 231):

```python
async def spawn_agent(
    ctx: RunContext[Deps],
    type: str,
    task: str,
    ...
    tier: str | None = None,
    thinking: str | None = None,
    ...
) -> str:
```

Docstring (after the `tier` paragraph, line ~301): add:

```python
    `thinking` overrides this spawn's reasoning effort — one of `"off"`,
    `"minimal"`, `"low"`, `"medium"`, `"high"`, `"xhigh"`. Omit it and the
    spawn inherits the sub-agent spec's own `thinking:` setting, then your
    session's level. `"off"` forces no reasoning effort for this spawn.
```

Foreground dispatch — thread `thinking` into the `run_subagent(...)` call (line ~362), at the tail after `tier`:

```python
    return await ctx.deps.services.run_subagent(
        type, task, mcp_names, max_output_chars, model,
        isolation, ctx.deps.subagent_depth, tier, thinking,
    )
```

Background dispatch — `_spawn_background` (line 151): add `thinking: str | None` to its signature and thread it into both `run_subagent_background(...)` calls (lines ~195, ~209), at the tail after `tier`. Pass `thinking=thinking` from `spawn_agent` into `_spawn_background(...)` (line ~354 area):

```python
            tier=tier,
            thinking=thinking,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_thinking_spawn_tool.py tests/test_spawn_tools.py tests/test_subagents.py -q`
Expected: all pass.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/tools/spawn_tools.py src/marim_harness/runtime/deps.py \
    tests/test_thinking_spawn_tool.py
git commit -m "feat(thinking): spawn_agent thinking override threaded to the runner seam"
```

---

### Task 8: Capability detection (`catalog.supports_thinking`)

**Files:**
- Modify: `src/marim_harness/workspace/catalog.py` (`ModelEntry` field ~line 32; `parse_models` ~line 62; new `model_supports_thinking` helper after `model_supports_images` ~line 260)
- Modify: `src/marim_harness/workspace/__init__.py` (export `model_supports_thinking`)
- Test: `tests/test_catalog.py` (append)

**Interfaces:**
- Produces: `ModelEntry.supports_thinking: bool | None`; `model_supports_thinking(entries, model_id) -> bool | None`.

Note (spec §8 reconciliation): pydantic-ai 2.8.0's `ModelProfile` IS a `TypedDict(total=False)` that declares `supports_thinking: bool` (profiles/__init__.py:95), so the spec's live `provider.model_profile(id).get('supports_thinking')` fallback is a real, valid API. It is intentionally deferred from v1 — not because it doesn't exist — to avoid threading a provider handle into the TUI picker; the catalog signal (OpenRouter `supported_parameters`) is sufficient for the annotate-only requirement. Detection stays catalog-only and best-effort: it annotates the UI but NEVER blocks selecting or applying a level. (The earlier "hasattr says it doesn't exist" reading was a mis-test — `hasattr` on an empty TypedDict instance is False for every key.)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_catalog.py`:

```python
def test_parse_models_reads_supported_parameters_reasoning():
    payload = {"data": [
        {"id": "a/thinks", "name": "Thinks",
         "supported_parameters": ["reasoning", "tools"]},
        {"id": "b/plain", "name": "Plain", "supported_parameters": ["tools"]},
        {"id": "c/unknown", "name": "Unknown"},
    ]}
    entries = {e.id: e for e in parse_models(payload)}
    assert entries["a/thinks"].supports_thinking is True
    assert entries["b/plain"].supports_thinking is False
    assert entries["c/unknown"].supports_thinking is None


def test_model_supports_thinking_lookup():
    from marim_harness.workspace.catalog import model_supports_thinking

    payload = {"data": [
        {"id": "a/thinks", "name": "T", "supported_parameters": ["reasoning"]},
    ]}
    entries = parse_models(payload)
    assert model_supports_thinking(entries, "a/thinks") is True
    assert model_supports_thinking(entries, "missing") is None
```

(`parse_models` is already imported at the top of `tests/test_catalog.py`; if not, add `from marim_harness.workspace.catalog import parse_models`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_catalog.py -k thinking -v`
Expected: FAIL — `ModelEntry` has no `supports_thinking`; `model_supports_thinking` is undefined.

- [ ] **Step 3: Implement**

`ModelEntry` (line ~17): add a field after `context_window` (line ~32):

```python
    # Whether the model accepts a reasoning-effort setting, per the catalog
    # (OpenRouter lists "reasoning" in supported_parameters). None when the
    # source doesn't say. Best-effort UI annotation only — NEVER a gate on
    # selecting or applying a thinking level (see the design spec §8).
    supports_thinking: bool | None = None
```

`parse_models` (line 39): after the `context_window = ...` line (line 61), read `supported_parameters`, and pass it into the `ModelEntry(...)`:

```python
        params = row.get("supported_parameters")
        supports_thinking: bool | None = None
        if isinstance(params, list):
            supports_thinking = "reasoning" in params
        entries.append(ModelEntry(id=model_id, name=display,
                                  supports_images=supports_images,
                                  context_window=context_window,
                                  supports_thinking=supports_thinking))
```

Add the helper after `model_supports_images` (line ~260):

```python
def model_supports_thinking(entries: list[ModelEntry], model_id: str) -> bool | None:
    """Whether ``model_id`` accepts a reasoning-effort setting per the catalog;
    None if the id is not present (capability unknown). Best-effort: a None or
    False here must NOT prevent a user from choosing a thinking level — it only
    annotates the picker."""
    for entry in entries:
        if entry.id == model_id:
            return entry.supports_thinking
    return None
```

`src/marim_harness/workspace/__init__.py`: add `model_supports_thinking` to the `from .catalog import (...)` block and to `__all__` (alongside `model_supports_images`, lines ~19 and ~61).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_catalog.py -q`
Expected: all pass (pre-existing catalog tests included).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/workspace/catalog.py src/marim_harness/workspace/__init__.py \
    tests/test_catalog.py
git commit -m "feat(thinking): best-effort supports_thinking detection in the model catalog"
```

---

### Task 9: `/think` command, picker, startup status

**Files:**
- Create: `src/marim_harness/interfaces/tui/thinking_picker.py`
- Modify: `src/marim_harness/interfaces/tui/commands.py` (handler after `_cmd_advisor` ~line 234; `COMMANDS` entry after `"advisor"` ~line 570; import)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`open_thinking_picker`/`_on_thinking_chosen` after `_on_advisor_chosen` ~line 904; startup notice in `on_mount` after the advisor notice ~line 235; import)
- Test: `tests/test_thinking_command.py`

**Interfaces:**
- Consumes: `Harness.set_thinking_level`, `Harness.thinking_level_id` (Task 4); `THINKING_LEVELS`, `parse_thinking_level` (Task 1).
- Produces: `HarnessApp.open_thinking_picker()`; `/think` (alias `effort`) in `COMMANDS`; `ThinkingPickerModal`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_thinking_command.py`:

```python
"""/think dispatch: a direct level, off, an unknown level, and blank-opens-picker."""

from types import SimpleNamespace

import pytest

from marim_harness.interfaces.tui.commands import COMMANDS_BY_NAME, dispatch


class _App:
    def __init__(self):
        self.posted: list[str] = []
        self.picker_opened = False
        self.calls: list = []
        self.harness = SimpleNamespace(
            set_thinking_level=lambda level: self.calls.append(level),
            thinking_level_id=None,
        )

    async def post_system(self, msg: str) -> None:
        self.posted.append(msg)

    async def open_thinking_picker(self) -> None:
        self.picker_opened = True


def test_think_command_registered():
    assert "think" in COMMANDS_BY_NAME
    assert "effort" in COMMANDS_BY_NAME  # alias


@pytest.mark.anyio
async def test_think_with_level_sets_it():
    app = _App()
    await dispatch(app, "/think high")
    assert app.calls == ["high"]
    assert any("high" in p for p in app.posted)


@pytest.mark.anyio
async def test_think_off_disables():
    app = _App()
    await dispatch(app, "/think off")
    assert app.calls == ["off"]


@pytest.mark.anyio
async def test_think_unknown_level_is_rejected_without_setting():
    app = _App()
    await dispatch(app, "/think ultra")
    assert app.calls == []
    assert any("ultra" in p or "unknown" in p.lower() for p in app.posted)


@pytest.mark.anyio
async def test_think_blank_opens_picker():
    app = _App()
    await dispatch(app, "/think")
    assert app.picker_opened
    assert app.calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_thinking_command.py -v`
Expected: FAIL — `"think" in COMMANDS_BY_NAME` is False; dispatch posts "Unknown command".

- [ ] **Step 3: Implement — commands.py**

Add the import near the top with the other package imports:

```python
from ...thinking import THINKING_LEVELS, parse_thinking_level
```

After `_cmd_advisor` (line ~234):

```python
async def _cmd_thinking(app: HarnessApp, arg: str) -> None:
    # Like /advisor, no mid-turn refusal: the level is read per round, so a
    # switch simply applies to the next turn/spawn.
    arg = arg.strip()
    if not arg:
        await app.open_thinking_picker()
        return
    level = parse_thinking_level(arg)
    if level is None:
        await app.post_system(
            f"Unknown thinking level {arg!r}. Choose one of: "
            f"{', '.join(THINKING_LEVELS)}."
        )
        return
    app.harness.set_thinking_level(level)
    await app.post_system(f"Thinking: **{level}** (persisted for this session)")
```

In `COMMANDS` after the `"advisor"` entry (line ~570):

```python
    Command("think", "set the thinking level: /think [level|off] (picker if blank)",
            _cmd_thinking, aliases=("effort",)),
```

(If `Command` has no `aliases` field, add the entry without it and register a second `Command("effort", ...)` line pointing at `_cmd_thinking`; match whatever the existing `Command` dataclass supports — inspect `COMMANDS`/`COMMANDS_BY_NAME` construction at lines ~556-600.)

- [ ] **Step 4: Implement — the picker modal**

Create `src/marim_harness/interfaces/tui/thinking_picker.py`:

```python
"""A modal for choosing the thinking level: a fixed six-item list (the thinking
vocabulary), unlike the model picker's dynamic catalog. Dismisses with the
chosen level, or None if cancelled."""

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ...thinking import THINKING_LEVELS


class ThinkingPickerModal(ModalScreen[str | None]):
    """Dismisses with the chosen thinking level, or None if cancelled."""

    CSS = """
    ThinkingPickerModal {
        align: center middle;
    }
    #thinking-box {
        width: 60%;
        max-width: 60;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #thinking-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #thinking-options {
        height: auto;
        max-height: 12;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, current: str | None = None) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="thinking-box"):
            title = "Select thinking level"
            if self.current:
                title += f"  (current: {self.current})"
            yield Static(title, id="thinking-title")
            options = OptionList(id="thinking-options")
            for level in THINKING_LEVELS:
                options.add_option(Option(level, id=level))
            yield options

    def on_mount(self) -> None:
        options = self.query_one("#thinking-options", OptionList)
        # Highlight the current level so Enter re-picks it by default.
        if self.current in THINKING_LEVELS:
            options.highlighted = THINKING_LEVELS.index(self.current)
        else:
            options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
```

- [ ] **Step 5: Implement — app.py**

Add the import near the other picker imports (alongside `from .model_picker import ModelPickerModal`):

```python
from .thinking_picker import ThinkingPickerModal
```

After `_on_advisor_chosen` (line ~904):

```python
    async def open_thinking_picker(self) -> None:
        """Fixed-list picker for the session thinking level. The choice lands
        on Harness.set_thinking_level (session-persisted, live)."""
        self.push_screen(
            ThinkingPickerModal(current=self.harness.thinking_level_id),
            self._on_thinking_chosen,
        )

    def _on_thinking_chosen(self, chosen: str | None) -> None:
        if not chosen:
            return
        self.harness.set_thinking_level(chosen)
        self._append_log(NoticeMessage(f"thinking: {chosen}"))
```

(`NoticeMessage` is already imported in app.py — used by `_on_model_chosen`/`_on_advisor_chosen`.)

In `on_mount`, after the advisor startup notice (line ~235):

```python
        # One-line thinking status at session start when a non-off level is
        # active (env default or session-persisted), so it's visible without
        # opening settings. off/unset stays silent (that's the default).
        level = self.harness.thinking_level_id
        if level is not None and level != "off":
            self._append_log(NoticeMessage(f"Thinking: {level} · /think"))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_thinking_command.py tests/test_commands.py tests/test_app.py -q`
Expected: all pass. (If a `test_commands.py` help-listing test asserts an exact command count, update it for the new entry + alias.)

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/tui/thinking_picker.py \
    src/marim_harness/interfaces/tui/commands.py \
    src/marim_harness/interfaces/tui/app.py tests/test_thinking_command.py
git commit -m "feat(thinking): /think command, level picker, and startup status line"
```

---

### Task 10: Settings-screen thinking row

**Files:**
- Modify: `src/marim_harness/interfaces/tui/settings.py` (`_tools_widgets` after the advisor row ~line 466; `_thinking_value_text` helper after `_advisor_value_text` ~line 596; `on_button_pressed` after the advisor branch ~line 605; picker/apply after `_on_advisor_chosen` ~line 854; imports ~line 36)
- Test: `tests/test_settings_screen.py` (append)

**Interfaces:**
- Consumes: `ModelConfig.thinking_level` (Task 2, via `env_cfg`); `save_env_settings` (already imported); `THINKING_LEVELS` (Task 1); `ThinkingPickerModal` (Task 9).
- Produces: widget ids `thinking-value`, `thinking-change`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_settings_screen.py` (reuses the file's existing `_Host`, `_fake_harness`, `_env_cfg`, `isolated_env` helpers):

```python
@pytest.mark.anyio
async def test_settings_has_thinking_row_defaulting_off():
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        value = str(app.screen.query_one("#thinking-value").render())
    assert value == "off"


@pytest.mark.anyio
async def test_thinking_choice_saves_env(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        app.screen._on_thinking_chosen("high")
        await pilot.pause()
        value = str(app.screen.query_one("#thinking-value").render())
    env_text = (tmp_path / "marim" / ".env").read_text()
    assert "MARIM_THINKING=high" in env_text
    assert os.environ.get("MARIM_THINKING") == "high"
    assert value == "high"


@pytest.mark.anyio
async def test_thinking_off_choice_drops_the_env_var(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("MARIM_THINKING", "high")
    env_cfg = _env_cfg()
    env_cfg.thinking_level = "high"
    app = _Host(_fake_harness(), env_cfg)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        app.screen._on_thinking_chosen("off")
        await pilot.pause()
        value = str(app.screen.query_one("#thinking-value").render())
    assert os.environ.get("MARIM_THINKING") is None
    assert value == "off"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_settings_screen.py -k thinking -v`
Expected: FAIL with `NoMatches` on `#thinking-value`.

- [ ] **Step 3: Implement**

Add the picker import near the top with the other settings imports (line ~36):

```python
from .thinking_picker import ThinkingPickerModal
```

In `_tools_widgets`, after the advisor row block (line ~466):

```python
        yield Static(
            "Thinking — reasoning effort applied to the model "
            "(off/minimal/low/medium/high/xhigh). This row saves the global "
            "default to .env (new sessions); /think overrides it per session, "
            "live. Not every provider supports it; unsupported models ignore it.",
            classes="muted",
        )
        with Horizontal(classes="srow"):
            yield Static("Thinking", classes="tier-row-label")
            yield Static(
                self._thinking_value_text(), id="thinking-value",
                classes="tier-row-value",
            )
            yield Button("change", id="thinking-change", variant="primary", compact=True)
```

Helper after `_advisor_value_text` (line ~596):

```python
    def _thinking_value_text(self) -> str:
        return self.env_cfg.thinking_level or "off"
```

`on_button_pressed`: add a branch after the advisor-change branch (line ~605):

```python
        elif bid == "thinking-change":
            self._open_thinking_picker()
```

Picker/apply methods after `_on_advisor_chosen` (line ~854):

```python
    def _open_thinking_picker(self) -> None:
        """Fixed-list picker for the global thinking default. The pick persists
        to .env (new sessions); the live per-session switch is /think."""
        self.app.push_screen(
            ThinkingPickerModal(current=self.env_cfg.thinking_level),
            self._on_thinking_chosen,
        )

    def _on_thinking_chosen(self, chosen: str | None) -> None:
        if not chosen:
            return
        try:
            if chosen == "off":
                # off DROPS the var rather than writing a sentinel: unset is the
                # env layer's own "no thinking", and a written "off" round-trips
                # to the same None anyway (parse_thinking_level("off") == "off",
                # but the .env default should read as absent).
                save_env_settings({}, drop=("MARIM_THINKING",))
                self.env_cfg.thinking_level = None
            else:
                save_env_settings({"MARIM_THINKING": chosen})
                self.env_cfg.thinking_level = chosen
        except Exception as exc:  # surface any write failure on the status line
            self._status(f"Save failed: {exc}")
            return
        self.query_one("#thinking-value", Static).update(self._thinking_value_text())
        self._status("✓ saved MARIM_THINKING · applies to new sessions")
```

Note: writing `MARIM_THINKING=off` would also work (parse folds it), but the advisor row's precedent is to DROP the var on an explicit off so the env layer reads as truly unset; this row follows that. The value cell then renders "off" via `_thinking_value_text` (`None or "off"`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_settings_screen.py -q`
Expected: all pass (pre-existing settings tests included).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/tui/settings.py tests/test_settings_screen.py
git commit -m "feat(thinking): settings-screen thinking row"
```

---

### Task 11: Sub-agent thinking UI display

**Files:**
- Modify: `src/marim_harness/runtime/deps.py` (`SubAgentThinkingCb` type alias after `SubAgentModelCb` ~line 49; `UIHooks.on_subagent_thinking` after `on_subagent_model` ~line 208)
- Modify: `src/marim_harness/subagents/runner.py` (`_prepare_spawn` — fire `on_subagent_thinking` alongside the resolved-model report ~line 730)
- Modify: `src/marim_harness/interfaces/tui/subagents/card.py` (`set_thinking_level` after `set_model` ~line 249; store label in `__init__` ~line 107)
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (`on_subagent_thinking` after `on_subagent_model` ~line 971; wire the callback wherever `on_subagent_model` is bound onto `UIHooks`)
- Test: `tests/test_thinking_subagent_ui.py`

**Interfaces:**
- Consumes: `resolve_thinking` (Task 1); `UIHooks.on_subagent_model` binding pattern.
- Produces: `SubAgentThinkingCb = Callable[[str, str], Awaitable[None]]`; `UIHooks.on_subagent_thinking`; `SubAgentWidget.set_thinking_level(level: str)`; `SubagentRunner._report_spawn_thinking(stream_id, override, spawn_defn)` (the extracted, unit-testable report).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_thinking_subagent_ui.py`:

```python
"""The sub-agent thinking level is reported to the UI and shown on the card
(only when a real level resolves — off/none stays silent). The report itself is
an extracted helper so it's testable without the full spawn machinery."""

from unittest.mock import MagicMock

import pytest
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings

from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.subagents.runner import SubagentRunner
from marim_harness.tools.provider import BuiltinToolProvider
from marim_harness.workspace.agents import AgentDef


def test_uihooks_has_on_subagent_thinking():
    hooks = UIHooks()
    assert hooks.on_subagent_thinking is None  # optional, None when no UI


def test_card_set_thinking_level_updates_label():
    from marim_harness.interfaces.tui.subagents.card import SubAgentWidget

    widget = SubAgentWidget("coder", "do it")
    widget.set_thinking_level("high")
    assert widget.thinking_label == "high"


def _runner(tmp_path, on_thinking, **kwargs) -> SubagentRunner:
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    deps.ui.on_subagent_thinking = on_thinking
    return SubagentRunner(
        BuiltinToolProvider(), MagicMock(), deps, MagicMock(), MagicMock(),
        get_model=lambda: TestModel(call_tools=[]),
        model_settings=ModelSettings(parallel_tool_calls=True),
        **kwargs,
    )


def _spec(thinking):
    return AgentDef(name="coder", description="", instructions="Go.",
                    tools=frozenset({"read_file"}), thinking=thinking)


@pytest.mark.anyio
async def test_report_fires_for_resolved_inherited_level(tmp_path):
    reported: list = []

    async def on_thinking(stream_id, level):
        reported.append((stream_id, level))

    runner = _runner(tmp_path, on_thinking, thinking_default=lambda: "high")
    await runner._report_spawn_thinking("stream-1", None, _spec(None))
    assert reported == [("stream-1", "high")]


@pytest.mark.anyio
async def test_report_override_beats_spec(tmp_path):
    reported: list = []

    async def on_thinking(stream_id, level):
        reported.append((stream_id, level))

    runner = _runner(tmp_path, on_thinking, thinking_default=lambda: "low")
    await runner._report_spawn_thinking("stream-1", "medium", _spec("high"))
    assert reported == [("stream-1", "medium")]


@pytest.mark.anyio
async def test_report_silent_for_off_and_none(tmp_path):
    reported: list = []

    async def on_thinking(stream_id, level):
        reported.append((stream_id, level))

    runner = _runner(tmp_path, on_thinking, thinking_default=lambda: "high")
    await runner._report_spawn_thinking("stream-1", "off", _spec(None))  # explicit off
    runner_none = _runner(tmp_path, on_thinking)  # no inherited, no spec
    await runner_none._report_spawn_thinking("stream-2", None, _spec(None))
    assert reported == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_thinking_subagent_ui.py -v`
Expected: FAIL — `UIHooks` has no `on_subagent_thinking`; `SubAgentWidget` has no `set_thinking_level`.

- [ ] **Step 3: Implement — deps.py**

In `src/marim_harness/runtime/deps.py`, after the `SubAgentModelCb` alias (line ~49):

```python
# (stream_id, level) -> None. Surfaces the resolved thinking level a spawn ran
# with (override → spec → inherited) so the card can annotate it. Fired only
# when a real level resolves; off/none stays silent. None when no UI.
SubAgentThinkingCb = Callable[[str, str], Awaitable[None]]
```

In `UIHooks`, after `on_subagent_model` (line ~208):

```python
    on_subagent_thinking: SubAgentThinkingCb | None = None
```

- [ ] **Step 4: Implement — runner.py**

Add the extracted report helper (placed near `_spawn_thinking_settings` from Task 6, e.g. just before `build`, line ~302):

```python
    async def _report_spawn_thinking(self, stream_id: str, override: str | None,
                                     spawn_defn) -> None:
        """Report the spawn's resolved thinking level to the UI, the same seam
        the model report uses — so the card can annotate the reasoning effort it
        actually ran with. Fires ONLY for a real level (off/none is the default;
        no annotation). Extracted so the resolve+fire is unit-testable without
        the full spawn machinery."""
        inherited = (
            self._thinking_default() if self._thinking_default is not None else None
        )
        level = resolve_thinking(
            override, spawn_defn.thinking if spawn_defn is not None else None, inherited
        )
        if not level or level == "off":
            return
        report = self.deps.ui.on_subagent_thinking
        if report is not None and stream_id:
            await report(stream_id, level)
```

In `_prepare_spawn` (line ~730), after the resolved-model report block (which ends at line ~735), call it. Reuse the `spawn_defn` already resolved at line ~721 and the `thinking` param added in Task 6:

```python
        await self._report_spawn_thinking(stream_id, thinking, spawn_defn)
```

- [ ] **Step 5: Implement — card.py + stream_render.py**

`src/marim_harness/interfaces/tui/subagents/card.py`, in `SubAgentWidget.__init__` (line ~96), initialize the label (after `self.model_label = model_label`, line ~107):

```python
        self.thinking_label = ""
```

After `set_model` (line ~249):

```python
    def set_thinking_level(self, level: str) -> None:
        """Annotate the reasoning effort this spawn ran with. Named
        set_thinking_level (not set_thinking) to avoid colliding with the
        streaming-reasoning sink's set_thinking(widget)."""
        self.thinking_label = level
        self._refresh_meta()
```

(Call whatever the card's existing meta-refresh method is — the one `set_model` calls at line ~249 to redraw the header. If `set_model` updates a specific `Static`/reactive, mirror that exact update for `thinking_label`; inspect `set_model` at lines 242-249.)

`src/marim_harness/interfaces/tui/stream_render.py`, after `on_subagent_model` (line ~971):

```python
    async def on_subagent_thinking(self, stream_id: str, level: str) -> None:
        """Relay the resolved sub-agent thinking level onto its card (and pane,
        if present), mirroring on_subagent_model."""
        parent = self._subagent_widgets.get(stream_id)
        if parent is not None:
            parent.set_thinking_level(level)
```

(Match `on_subagent_model`'s exact widget-lookup pattern at lines 961-971 — use the same `self._subagent_widgets`/`.get` accessor and the same pane-forwarding if it forwards to `parent.pane`.)

Wire the callback onto `UIHooks` wherever `on_subagent_model` is bound (grep for `on_subagent_model=` in the TUI wiring — likely `app.py`/`stream_render.py` `bind_ui` path — and add `on_subagent_thinking=<sink>.on_subagent_thinking` alongside it).

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_thinking_subagent_ui.py tests/test_subagents.py tests/test_app.py -q`
Expected: all pass.

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/deps.py src/marim_harness/subagents/runner.py \
    src/marim_harness/interfaces/tui/subagents/card.py \
    src/marim_harness/interfaces/tui/stream_render.py tests/test_thinking_subagent_ui.py
git commit -m "feat(thinking): report and display the resolved sub-agent thinking level"
```

---

### Task 12: Headless `--think` flag, docs, full CI parity

**Files:**
- Modify: `src/marim_harness/interfaces/cli/default_cmd.py` (`_build_parser` ~line 26-58; `run_default` — set env before `build_harness`, ~line 102+)
- Modify: `CLAUDE.md` (Supporting subsystems section)
- Test: `tests/test_default_cmd.py` (append; create if absent)

**Interfaces:**
- Consumes: `THINKING_LEVELS` (Task 1); `MARIM_THINKING` → bootstrap (Task 2/5).
- Produces: `marim --think <level>` sets `os.environ["MARIM_THINKING"]` before `build_harness`, flowing through bootstrap → builder.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_default_cmd.py`:

```python
def test_think_flag_sets_env(monkeypatch):
    from marim_harness.interfaces.cli.default_cmd import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["--think", "high"])
    assert args.think == "high"


def test_think_flag_choices_reject_unknown():
    import pytest as _pytest
    from marim_harness.interfaces.cli.default_cmd import _build_parser

    parser = _build_parser()
    with _pytest.raises(SystemExit):
        parser.parse_args(["--think", "ultra"])
```

(If `_build_parser` is named differently or the parser is built inline in `run_default`, extract it to a module-level `_build_parser()` first — the advisor/headless flags follow that shape — and adapt. Inspect `_build_parser`/`run_default` at lines 26-58 / 102+.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_default_cmd.py -k think -v`
Expected: FAIL — `Namespace` has no attribute `think` (or `--think` is an unrecognized argument).

- [ ] **Step 3: Implement — default_cmd.py**

Add the import near the top with the other package imports:

```python
from ...thinking import THINKING_LEVELS
```

In `_build_parser` (line ~26), add the flag near the other model flags:

```python
    parser.add_argument(
        "--think",
        choices=THINKING_LEVELS,
        default=None,
        help="thinking level (reasoning effort) for this run: "
        "off/minimal/low/medium/high/xhigh. Overrides MARIM_THINKING.",
    )
```

In `run_default` (line ~102+), BEFORE the first `build_harness(...)` call (build_harness reads `MARIM_THINKING` via bootstrap → config), set the env var when the flag is present:

```python
    # --think seeds MARIM_THINKING so the level flows through the normal
    # bootstrap → config → builder path (no separate wiring). A new session
    # then persists it; an existing session's saved level still wins (the
    # session override beats the env default — see Harness._resolve_thinking_id).
    if args.think is not None:
        os.environ["MARIM_THINKING"] = args.think
```

(Ensure `import os` is present at the top of the module.)

- [ ] **Step 4: CLAUDE.md**

In `CLAUDE.md`, add one bullet to "Supporting subsystems (one concern each)" after the `advisor.py` bullet:

```markdown
- `thinking.py` (root) — thinking level (reasoning effort): one ordered
  vocabulary (`off/minimal/low/medium/high/xhigh`) and three pure helpers —
  `parse_thinking_level` (env/CLI/`/think` coercion), `settings_for` (fold a
  level into `ModelSettings.thinking`; `off`/unset OMITS the key, byte-identical
  to pre-thinking behavior), and `resolve_thinking` (sub-agent precedence:
  spawn override → spec `thinking:`/`effort:` → inherited session level). The
  main loop applies it per turn in `TurnController._turn_model_settings`; the
  level persists on `SessionStore.thinking` and lives on `Harness.thinking_level_id`
  (read lazily by the controller closure and the sub-agent runner, so `/think`
  switches without a rebuild). Seeded by `MARIM_THINKING` / `--think`; TUI
  `/think` command + Settings row. Under the `claude-cli` main provider it's a
  documented no-op (marim's `ModelSettings` don't reach Claude Code). Detection
  (`catalog.supports_thinking`) is best-effort UI annotation only — it never
  blocks a level.
```

- [ ] **Step 5: Run the new test + full CI parity**

```bash
uv run pytest --no-cov tests/test_default_cmd.py -k think -v
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: the flag test passes; all three gates clean/green (coverage runs by default — fine).

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/cli/default_cmd.py CLAUDE.md tests/test_default_cmd.py
git commit -m "feat(thinking): headless --think flag + CLAUDE.md subsystem note"
```

- [ ] **Step 7: Manual smoke recipe (record in PR notes, do NOT run unattended)**

Free-local-only smoke (per the no-paid-models rule; requires LM Studio serving `ornith-1.0-9b`):

```bash
MARIM_PROVIDER=local MARIM_THINKING=high uv run marim
# In-session: confirm the startup "Thinking: high · /think" line; run /think
# medium and confirm the confirmation line; open Settings → Tools and confirm
# the Thinking row reads the saved default; spawn a sub-agent and confirm its
# card annotates the resolved level. (A local model may ignore the setting —
# that's the expected best-effort behavior.)
```

---

## Out of scope (tracked in the spec)

Per-provider thinking-token budgets; the live `ModelProfile.get('supports_thinking')` fallback (a real pydantic-ai 2.8.0 API — `ModelProfile` is a `TypedDict` with that key — but deferred to avoid threading a provider into the TUI picker; detection stays catalog-only for v1); forcing thinking under the `claude-cli` main provider; streaming-reasoning display changes (the `set_thinking(widget)` sink is unrelated).
