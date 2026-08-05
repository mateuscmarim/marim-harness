# Tools Settings Page Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Polish the TUI Tools settings page: fix picker-row CSS, group controls under headers, compact rows, dock a focus-driven help line, and dim+disable dependent controls while their master is off.

**Architecture:** Keep the existing three-way split. `settings_env.py` gains pure registries and helpers (`FIELD_HELP`, `SECTION_HELP`, dependency maps, `help_for`, `dependents_enabled`) with no Textual. `settings_sections.py` restructures only `tools_widgets` (group headers, `row-*` wrappers, compact inputs, horizontal tool-search radio) and renames tier label/value classes to shared `.row-label`/`.row-value`. `settings.py` owns CSS (scoped to `#section-tools` where layout changes must not leak), mounts `#settings-help`, refreshes help on focus/section change, and applies dependency state on mount and master changes. Control widget ids stay stable so every existing autosave handler and test keeps working.

**Tech Stack:** Python 3.10+, Textual, pytest (anyio), uv.

## Global Constraints

- Python `>=3.10`; no 3.11+-only syntax.
- `uv` for everything (`uv run …`); never bare `python`/`pip`/`pytest`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity ≤ 10.
- CI order before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- Env var names and persistence semantics are unchanged.
- Existing control widget ids are unchanged (`sw-lsp`, `sw-lsp-tools`, `toolsearch-set`, `toolsearch-threshold`, `sw-job`, `sw-workflows`, `subagent-req-limit`, `wake-depth-cap`, `sw-tiering`, `tier-value-{tier}`, `tier-change-{tier}`, `advisor-value`, `advisor-change`, `advisor-max-tokens`, `advisor-max-uses`, `thinking-value`, `thinking-change`). Only *new* container ids (`row-*`) and class renames (`.tier-row-label` → `.row-label`, `.tier-row-value` → `.row-value`) are allowed.
- Pure helpers stay side-effect-free and unit-tested directly.
- Compact `.frow` height and horizontal tool-search RadioSet are scoped under `#section-tools` only — Context & Memory / Notifications / Session mode radios must keep today's layout.
- Spec: `docs/superpowers/specs/2026-08-04-tools-settings-polish-design.md`.

## File Structure

- **Modify** `src/marim_harness/interfaces/tui/settings_env.py` — pure registries + `help_for` + `dependents_enabled`.
- **Modify** `src/marim_harness/interfaces/tui/settings_sections.py` — `group_header`, restructured `tools_widgets` / `_tier_widgets` / `_advisor_widgets` / `_thinking_widgets`; Session model row gets `model-label` class.
- **Modify** `src/marim_harness/interfaces/tui/settings.py` — CSS; mount `#settings-help`; help refresh; `_refresh_dependencies`; wire master-change paths; imports from `settings_env`.
- **Modify** `tests/test_settings_screen.py` — update `.tier-row-label` queries; add help-line + dependency screen tests.
- **Create** `tests/test_settings_env_helpers.py` — pure unit tests for the new helpers.

---

### Task 1: Pure help + dependency helpers

**Files:**
- Modify: `src/marim_harness/interfaces/tui/settings_env.py`
- Create: `tests/test_settings_env_helpers.py`

**Interfaces:**
- Consumes: nothing new (stdlib `collections.abc` only).
- Produces:
  - `FIELD_HELP: dict[str, str]` — keys and copy exactly as the spec help table (including separate entries for `tier-change-cheap`, `tier-change-med`, `tier-change-high`).
  - `SECTION_HELP: dict[str, str]` — at least `{"tools": "Env-backed settings — save automatically on change; apply next launch unless the field says live."}`.
  - `CHECK_DEPENDENTS: dict[str, list[str]]` =
    `{"sw-lsp": ["row-lsp-tools"], "sw-tiering": ["row-tier-cheap", "row-tier-med", "row-tier-high"]}`.
  - `VALUE_DEPENDENTS: dict[str, tuple[list[str], Callable[[str], bool]]]` =
    `{"toolsearch": (["row-toolsearch-threshold"], lambda v: v != "off"), "advisor": (["row-advisor-tokens", "row-advisor-uses"], lambda v: v != "off")}`.
  - `help_for(ids: Iterable[str]) -> str | None` — first id present in `FIELD_HELP`, else `None`.
  - `dependents_enabled(check_values: Mapping[str, bool], value_masters: Mapping[str, str]) -> dict[str, bool]` — every `row-*` id from both registries mapped to enabled bool. Missing check masters default to `False` (disabled dependents). Missing value masters default to `""` (predicate decides; `"" != "off"` is True so threshold/advisor knobs enable unless explicitly `"off"` — tests lock this: pass `"off"` to disable).

- [ ] **Step 1: Write the failing pure tests**

Create `tests/test_settings_env_helpers.py`:

```python
"""Pure helpers for the Tools settings help line and dependency dimming."""

from marim_harness.interfaces.tui.settings_env import (
    CHECK_DEPENDENTS,
    FIELD_HELP,
    SECTION_HELP,
    VALUE_DEPENDENTS,
    dependents_enabled,
    help_for,
)


def test_help_for_direct_id():
    assert help_for(["sw-lsp"]) == FIELD_HELP["sw-lsp"]


def test_help_for_first_matching_ancestor_wins():
    # Focused RadioButton id first, then parent RadioSet — set wins.
    text = help_for(["toolsearch-auto", "toolsearch-set", "section-tools"])
    assert text == FIELD_HELP["toolsearch-set"]


def test_help_for_unknown_returns_none():
    assert help_for(["nope", "also-nope"]) is None
    assert help_for([]) is None


def test_section_help_tools_present():
    assert "tools" in SECTION_HELP
    assert "save automatically" in SECTION_HELP["tools"].lower()


def test_field_help_covers_every_tools_control():
    required = {
        "sw-lsp",
        "sw-lsp-tools",
        "toolsearch-set",
        "toolsearch-threshold",
        "sw-job",
        "sw-workflows",
        "subagent-req-limit",
        "wake-depth-cap",
        "sw-tiering",
        "tier-change-cheap",
        "tier-change-med",
        "tier-change-high",
        "advisor-change",
        "advisor-max-tokens",
        "advisor-max-uses",
        "thinking-change",
    }
    assert required <= set(FIELD_HELP)


def test_dependents_enabled_checkbox_masters():
    enabled = dependents_enabled(
        {"sw-lsp": False, "sw-tiering": True},
        {"toolsearch": "auto", "advisor": "off"},
    )
    assert enabled["row-lsp-tools"] is False
    assert enabled["row-tier-cheap"] is True
    assert enabled["row-tier-med"] is True
    assert enabled["row-tier-high"] is True
    assert enabled["row-toolsearch-threshold"] is True
    assert enabled["row-advisor-tokens"] is False
    assert enabled["row-advisor-uses"] is False


def test_dependents_enabled_toolsearch_off_disables_threshold():
    enabled = dependents_enabled(
        {"sw-lsp": True, "sw-tiering": True},
        {"toolsearch": "off", "advisor": "openrouter/x"},
    )
    assert enabled["row-toolsearch-threshold"] is False
    assert enabled["row-advisor-tokens"] is True


def test_check_and_value_registries_list_expected_rows():
    check_rows = {r for rows in CHECK_DEPENDENTS.values() for r in rows}
    value_rows = {r for rows, _ in VALUE_DEPENDENTS.values() for r in rows}
    assert check_rows == {
        "row-lsp-tools",
        "row-tier-cheap",
        "row-tier-med",
        "row-tier-high",
    }
    assert value_rows == {
        "row-toolsearch-threshold",
        "row-advisor-tokens",
        "row-advisor-uses",
    }
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest --no-cov tests/test_settings_env_helpers.py -v`

Expected: FAIL with `ImportError` / `cannot import name 'help_for'`.

- [ ] **Step 3: Implement registries + helpers in `settings_env.py`**

Append after the existing `TIER_ENV` block (keep module free of Textual). Use the exact help strings from the spec table. Implementation sketch:

```python
from collections.abc import Callable, Iterable, Mapping

FIELD_HELP: dict[str, str] = {
    "sw-lsp": (
        "Language-server integration (diagnostics on edit). Applies next launch."
    ),
    "sw-lsp-tools": (
        "Six navigation tools (definitions, references, …). Requires LSP. "
        "Applies next launch."
    ),
    "toolsearch-set": (
        "Serve MCP/plugin tools via the search_tools tool instead of up-front "
        "schemas. 'auto' activates once the tool count passes the threshold. "
        "Applies next launch."
    ),
    "toolsearch-threshold": (
        "Tool count at which 'auto' tool search activates. Applies next launch."
    ),
    "sw-job": (
        "One combined job tool instead of separate list/output/wait/cancel "
        "tools. Applies next launch."
    ),
    "sw-workflows": (
        "Model-authored Python workflows in a sandbox (run_workflow). "
        "Applies live."
    ),
    "subagent-req-limit": (
        "Maximum model requests per sub-agent run. Applies next launch."
    ),
    "wake-depth-cap": (
        "Maximum autonomous turns after a finished job wakes the agent. "
        "Applies next launch."
    ),
    "sw-tiering": (
        "Route new spawns to cheap/med/high tier models. Off sends every spawn "
        "to the main model; tier picks stay saved. Applies live to new spawns."
    ),
    "tier-change-cheap": (
        "Model for cheap-tier spawns; unset inherits the main model. "
        "Saves to .env — applies to new sessions."
    ),
    "tier-change-med": (
        "Model for med-tier spawns; unset inherits the main model. "
        "Saves to .env — applies to new sessions."
    ),
    "tier-change-high": (
        "Model for high-tier spawns; unset inherits the main model. "
        "Saves to .env — applies to new sessions."
    ),
    "advisor-change": (
        "A model the agent can consult mid-task for strategic guidance. "
        "Saves the global default to .env (new sessions); /advisor overrides "
        "per session, live. Type 'off' to clear."
    ),
    "advisor-max-tokens": "Token cap on advisor replies. Applies next launch.",
    "advisor-max-uses": (
        "Advisor calls per turn; 0 = unlimited. Applies next launch."
    ),
    "thinking-change": (
        "Reasoning effort (off/minimal/low/medium/high/xhigh). Saves the "
        "global default to .env (new sessions); /think overrides per session, "
        "live. Unsupported models ignore it."
    ),
}

SECTION_HELP: dict[str, str] = {
    "tools": (
        "Env-backed settings — save automatically on change; apply next launch "
        "unless the field says live."
    ),
}

CHECK_DEPENDENTS: dict[str, list[str]] = {
    "sw-lsp": ["row-lsp-tools"],
    "sw-tiering": ["row-tier-cheap", "row-tier-med", "row-tier-high"],
}

VALUE_DEPENDENTS: dict[str, tuple[list[str], Callable[[str], bool]]] = {
    "toolsearch": (["row-toolsearch-threshold"], lambda v: v != "off"),
    "advisor": (
        ["row-advisor-tokens", "row-advisor-uses"],
        lambda v: v != "off",
    ),
}


def help_for(ids: Iterable[str]) -> str | None:
    """Return FIELD_HELP for the first id in ``ids`` that has an entry."""
    for widget_id in ids:
        text = FIELD_HELP.get(widget_id)
        if text is not None:
            return text
    return None


def dependents_enabled(
    check_values: Mapping[str, bool],
    value_masters: Mapping[str, str],
) -> dict[str, bool]:
    """Map every dependent ``row-*`` id to whether its controls should be enabled."""
    enabled: dict[str, bool] = {}
    for master, rows in CHECK_DEPENDENTS.items():
        on = bool(check_values.get(master, False))
        for row in rows:
            enabled[row] = on
    for master, (rows, pred) in VALUE_DEPENDENTS.items():
        on = pred(value_masters.get(master, ""))
        for row in rows:
            enabled[row] = on
    return enabled
```

Update the module docstring one line to mention help/dependency registries.

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest --no-cov tests/test_settings_env_helpers.py -v`

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/settings_env.py tests/test_settings_env_helpers.py
git commit -m "feat(tui): pure help and dependency helpers for Tools settings"
```

---

### Task 2: Restructure Tools widget tree + CSS layout fixes

**Files:**
- Modify: `src/marim_harness/interfaces/tui/settings_sections.py`
- Modify: `src/marim_harness/interfaces/tui/settings.py` (CSS only in this task)
- Modify: `tests/test_settings_screen.py` (class-name query + new DOM assertions)

**Interfaces:**
- Consumes: Task 1 registries only for documentation parity (widget tree does not import `FIELD_HELP`; row ids must match `CHECK_DEPENDENTS` / `VALUE_DEPENDENTS`).
- Produces:
  - `group_header(text: str) -> ComposeResult` yielding `Static(text, classes="group-head")`.
  - `tools_widgets` layout with six groups; no top banner; no tier/advisor/thinking prose `Static`s.
  - Dependent wrappers: `#row-lsp-tools`, `#row-toolsearch-threshold`, `#row-tier-cheap|med|high`, `#row-advisor-tokens`, `#row-advisor-uses` (class `dep-row` for padding).
  - Classes: `.row-label` (was `.tier-row-label`), `.row-value` (was `.tier-row-value`), `.num` on Tools integer Inputs, `#model-label` keeps id and gains class `model-label`.
  - CSS in `SettingsScreen.CSS`: remove `.srow Static { width: auto }`; add rules listed in Step 3.

- [ ] **Step 1: Write failing DOM tests**

In `tests/test_settings_screen.py`, change `test_settings_has_three_tier_rows` to query `.row-label` instead of `.tier-row-label`.

Add:

```python
@pytest.mark.anyio
async def test_tools_page_has_group_headers_and_dep_rows():
    """Tools page mounts headed groups and every dependency row wrapper."""
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        s = app.screen
        headers = {str(w.render()) for w in s.query("#section-tools .group-head")}
        assert {
            "Language server",
            "Tool search",
            "Agent tools",
            "Sub-agents",
            "Advisor",
            "Thinking",
        } <= headers
        for row_id in (
            "row-lsp-tools",
            "row-toolsearch-threshold",
            "row-tier-cheap",
            "row-tier-med",
            "row-tier-high",
            "row-advisor-tokens",
            "row-advisor-uses",
        ):
            assert s.query_one(f"#section-tools #{row_id}") is not None
        # Control ids stay stable inside the wrappers.
        assert s.query_one("#row-lsp-tools #sw-lsp-tools") is not None
        assert s.query_one("#row-toolsearch-threshold #toolsearch-threshold") is not None
        assert s.query_one("#row-tier-cheap #tier-change-cheap") is not None
        assert s.query_one("#row-advisor-tokens #advisor-max-tokens") is not None
        # Banner / prose walls removed.
        body = " ".join(str(w.render()) for w in s.query("#section-tools Static"))
        assert "Saved to .env" not in body
        assert "Master switch" not in body
        assert "Advisor — a model" not in body
        assert "Thinking — reasoning" not in body
        # Session model row still exists with model-label class.
        assert "model-label" in s.query_one("#model-label").classes
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest --no-cov tests/test_settings_screen.py::test_tools_page_has_group_headers_and_dep_rows tests/test_settings_screen.py::test_settings_has_three_tier_rows -v`

Expected: FAIL (no `.group-head` / `row-*` / `.row-label`).

- [ ] **Step 3: Update CSS in `settings.py`**

Replace the `.srow` / tier / `.frow` block inside `SettingsScreen.CSS` with:

```css
    .srow { width: 1fr; height: 1; }
    .srow Button { width: auto; height: 1; border: none; padding: 0 1; margin-left: 2; }
    .model-label { width: auto; }
    .row-label { width: 24; }
    .row-value { width: 1fr; color: $text-muted; }
    .frow { width: 1fr; height: 3; }
    .frow Label { width: 24; height: 3; content-align: left middle; }
    .frow Input { width: 1fr; }
    /* Tools-only compact rows — do not change Context/Notifications .frow. */
    #section-tools .frow { height: 1; }
    #section-tools .frow Label { height: 1; content-align: left middle; }
    #section-tools .num {
        width: 14;
        height: 1;
        border: none;
        background: $panel;
        padding: 0 1;
    }
    #section-tools .num:focus { border-bottom: tall $accent; }
    #section-tools #toolsearch-set { layout: horizontal; height: 1; width: auto; }
    #section-tools .group-head {
        color: $accent;
        text-style: bold;
        margin-top: 1;
        height: 1;
    }
    #section-tools .group-head:first-child { margin-top: 0; }
    #section-tools .dep-row { padding-left: 2; height: auto; }
    #section-tools .dep-row.dimmed { color: $text-muted; text-style: dim; }
    #settings-help {
        height: auto;
        max-height: 2;
        padding: 0 2;
        color: $text-muted;
        background: $surface;
    }
```

Note: `#settings-help` CSS lands now so Task 3 only mounts/wires it; leave the widget out of `compose` until Task 3 if preferred, or mount an empty hidden Static — either is fine as long as Task 2 tests do not require it.

- [ ] **Step 4: Restructure `settings_sections.py`**

1. Add helper:

```python
def group_header(text: str) -> ComposeResult:
    yield Static(text, classes="group-head")
```

2. In `session_widgets`, change the model label to:

```python
yield Static(f"Model: {harness.model_label}", id="model-label", classes="model-label")
```

3. Replace `tools_widgets` + `_tier_widgets` + `_advisor_widgets` + `_thinking_widgets` with the grouped structure. Full target for `tools_widgets`:

```python
def tools_widgets(env_cfg: ModelConfig) -> ComposeResult:
    """The Tools page: headed groups, compact rows, dependent wrappers.

    Prose walls live on the screen's docked help line (FIELD_HELP), not inline.
    """
    yield from group_header("Language server")
    yield BoxCheckbox("LSP", value=env_cfg.lsp_enabled, id="sw-lsp")
    with Horizontal(id="row-lsp-tools", classes="dep-row"):
        yield BoxCheckbox(
            "LSP navigation tools",
            value=env_cfg.lsp_tools_enabled,
            id="sw-lsp-tools",
        )

    yield from group_header("Tool search")
    yield Label("Tool search (MCP/plugin tools)")
    with RadioSet(id="toolsearch-set"):
        for name in TOOL_SEARCH_MODES:
            yield RadioButton(
                name,
                value=(name == env_cfg.tool_search),
                id=f"toolsearch-{name}",
            )
    with Horizontal(id="row-toolsearch-threshold", classes="frow dep-row"):
        yield Label("Tool-search threshold")
        yield Input(
            value=str(env_cfg.tool_search_threshold),
            id="toolsearch-threshold",
            type="integer",
            classes="num",
        )

    yield from group_header("Agent tools")
    yield BoxCheckbox("Job tool combined", value=env_cfg.job_tool_combined, id="sw-job")
    yield BoxCheckbox(
        "Dynamic workflows (run_workflow)",
        value=env_cfg.workflows_enabled,
        id="sw-workflows",
    )

    yield from group_header("Sub-agents")
    with Horizontal(classes="frow"):
        yield Label("Sub-agent request limit")
        yield Input(
            value=str(env_cfg.subagent.request_limit),
            id="subagent-req-limit",
            type="integer",
            classes="num",
        )
    with Horizontal(classes="frow"):
        yield Label("Autonomous wake turns")
        yield Input(
            value=str(env_cfg.subagent.wake_depth_cap),
            id="wake-depth-cap",
            type="integer",
            classes="num",
        )
    yield BoxCheckbox(
        "Model tiering",
        value=env_cfg.subagent.tiers.enabled,
        id="sw-tiering",
    )
    yield from _tier_widgets(env_cfg)
    yield from _advisor_widgets(env_cfg)
    yield from _thinking_widgets(env_cfg)


def _tier_widgets(env_cfg: ModelConfig) -> ComposeResult:
    for tier, _env_key, label in TIER_ROWS:
        with Horizontal(id=f"row-tier-{tier}", classes="srow dep-row"):
            yield Static(label, classes="row-label")
            yield Static(
                tier_value_text(env_cfg, tier),
                id=f"tier-value-{tier}",
                classes="row-value",
            )
            yield Button(
                "change", id=f"tier-change-{tier}", variant="primary", compact=True
            )


def _advisor_widgets(env_cfg: ModelConfig) -> ComposeResult:
    yield from group_header("Advisor")
    with Horizontal(classes="srow"):
        yield Static("Advisor", classes="row-label")
        yield Static(
            advisor_value_text(env_cfg), id="advisor-value", classes="row-value"
        )
        yield Button("change", id="advisor-change", variant="primary", compact=True)
    with Horizontal(id="row-advisor-tokens", classes="frow dep-row"):
        yield Label("Advisor max tokens")
        yield Input(
            value=str(env_cfg.advisor_max_tokens),
            id="advisor-max-tokens",
            type="integer",
            classes="num",
        )
    with Horizontal(id="row-advisor-uses", classes="frow dep-row"):
        yield Label("Advisor max uses/turn")
        yield Input(
            value=str(env_cfg.advisor_max_uses or 0),
            id="advisor-max-uses",
            type="integer",
            classes="num",
        )


def _thinking_widgets(env_cfg: ModelConfig) -> ComposeResult:
    yield from group_header("Thinking")
    with Horizontal(classes="srow"):
        yield Static("Thinking", classes="row-label")
        yield Static(
            thinking_value_text(env_cfg), id="thinking-value", classes="row-value"
        )
        yield Button("change", id="thinking-change", variant="primary", compact=True)
```

Search the repo for `.tier-row-label` / `.tier-row-value` and update any remaining references (tests + CSS only expected).

- [ ] **Step 5: Run targeted tests — expect PASS**

Run:

```bash
uv run pytest --no-cov \
  tests/test_settings_screen.py::test_tools_page_has_group_headers_and_dep_rows \
  tests/test_settings_screen.py::test_settings_has_three_tier_rows \
  tests/test_settings_screen.py::test_every_page_mounts_its_fields \
  tests/test_settings_screen.py::test_tier_rows_show_inherit_main_by_default \
  -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/settings_sections.py \
  src/marim_harness/interfaces/tui/settings.py \
  tests/test_settings_screen.py
git commit -m "feat(tui): regroup Tools settings with compact aligned rows"
```

---

### Task 3: Docked help line wiring

**Files:**
- Modify: `src/marim_harness/interfaces/tui/settings.py`
- Modify: `tests/test_settings_screen.py`

**Interfaces:**
- Consumes: `FIELD_HELP`, `SECTION_HELP`, `help_for` from Task 1.
- Produces:
  - Widget `#settings-help` (`Static`, between `#settings-body` and `#settings-footer`).
  - `_refresh_help() -> None` — if `self.focused` is under `#settings-content`, walk focused→ancestors collecting ids and set help to `help_for(ids)`; else if `SECTION_HELP.get(self.active_section)` show that; else clear/hide.
  - Called from `on_mount` (after `_ready = True`), `watch_active_section` / `_apply_section`, and on focus/blur of content descendants.
  - Prefer Textual events: `on_descendant_focus` / `on_descendant_blur` on the screen (or listen when `event.widget` is inside `#settings-content`). Always call `_refresh_help` after `set_focus(None)` paths that matter (escape-to-rail already blurs).

- [ ] **Step 1: Write failing help-line tests**

```python
@pytest.mark.anyio
async def test_tools_section_shows_section_help_on_rail():
    from marim_harness.interfaces.tui.settings_env import SECTION_HELP

    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        # Rail: nothing focused.
        app.screen.set_focus(None)
        await pilot.pause()
        help_w = app.screen.query_one("#settings-help")
        assert str(help_w.render()) == SECTION_HELP["tools"]
        # Session has no SECTION_HELP → empty.
        app.screen.active_section = "session"
        await pilot.pause()
        assert str(app.screen.query_one("#settings-help").render()) == ""


@pytest.mark.anyio
async def test_focusing_tools_field_shows_field_help():
    from marim_harness.interfaces.tui.settings_env import FIELD_HELP

    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        app.screen.query_one("#sw-lsp").focus()
        await pilot.pause()
        assert str(app.screen.query_one("#settings-help").render()) == FIELD_HELP["sw-lsp"]
        # Escape returns to rail → section help again.
        await pilot.press("escape")
        await pilot.pause()
        from marim_harness.interfaces.tui.settings_env import SECTION_HELP

        assert str(app.screen.query_one("#settings-help").render()) == SECTION_HELP["tools"]
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest --no-cov tests/test_settings_screen.py::test_tools_section_shows_section_help_on_rail tests/test_settings_screen.py::test_focusing_tools_field_shows_field_help -v`

Expected: FAIL (`#settings-help` missing or empty when it should not be).

- [ ] **Step 3: Implement help line in `settings.py`**

1. Extend imports from `settings_env`:

```python
from .settings_env import (
    ...
    FIELD_HELP,  # only if needed; prefer help_for/SECTION_HELP
    SECTION_HELP,
    help_for,
)
```

2. In `compose`, between the body `Horizontal` and the footer:

```python
yield Static("", id="settings-help")
```

3. Add methods:

```python
def _ancestor_ids(self, widget) -> list[str]:
    ids: list[str] = []
    node = widget
    while node is not None and node is not self:
        wid = getattr(node, "id", None)
        if wid:
            ids.append(wid)
        node = node.parent
    return ids

def _refresh_help(self) -> None:
    help_w = self.query_one("#settings-help", Static)
    focused = self.focused
    text = ""
    if focused is not None:
        # Only content-pane focus shows field help (not rail chrome).
        content = self.query_one("#settings-content")
        node = focused
        inside = False
        while node is not None:
            if node is content:
                inside = True
                break
            node = node.parent
        if inside:
            text = help_for(self._ancestor_ids(focused)) or ""
    if not text:
        text = SECTION_HELP.get(self.active_section, "")
    help_w.update(text)
    help_w.display = bool(text)
```

4. Call `_refresh_help()` at end of `on_mount` (after `_ready = True`), end of `_apply_section`, and from:

```python
def on_descendant_focus(self, event) -> None:
    self._refresh_help()

def on_descendant_blur(self, event) -> None:
    # Defer one tick so the next focus (if any) wins the race.
    self.call_after_refresh(self._refresh_help)
```

Use the real Textual event types your version exports (`events.DescendantFocus` / `DescendantBlur` if available; otherwise the handlers above). If `on_descendant_blur` races, the `call_after_refresh` path is required.

5. Keep footer `#settings-status` behavior unchanged — never write help text there.

- [ ] **Step 4: Run tests — expect PASS**

Run:

```bash
uv run pytest --no-cov \
  tests/test_settings_screen.py::test_tools_section_shows_section_help_on_rail \
  tests/test_settings_screen.py::test_focusing_tools_field_shows_field_help \
  tests/test_settings_screen.py::test_escape_returns_to_rail_before_closing \
  -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/settings.py tests/test_settings_screen.py
git commit -m "feat(tui): docked focus-driven help line on Tools settings"
```

---

### Task 4: Dependency dimming wiring

**Files:**
- Modify: `src/marim_harness/interfaces/tui/settings.py`
- Modify: `tests/test_settings_screen.py`

**Interfaces:**
- Consumes: `dependents_enabled`, `CHECK_DEPENDENTS`, `VALUE_DEPENDENTS` from Task 1; `advisor_value_text` already imported path via sections helpers / local methods.
- Produces:
  - `_refresh_dependencies() -> None` reading live checkbox/radio/`env_cfg` state, calling `dependents_enabled`, then for each `row_id, on` pair: `row.set_class(not on, "dimmed")` and set `disabled=not on` on every focusable child (`BoxCheckbox`, `Input`, `Button`).
  - Called from `on_mount` (after widgets ready), after `sw-lsp` env checkbox commit path, after `_toggle_tiering`, after `toolsearch-set` radio commit, after `_on_advisor_chosen`.
  - Master value sources:
    - checks: `query_one("#sw-lsp", BoxCheckbox).value`, `query_one("#sw-tiering", BoxCheckbox).value`
    - toolsearch: selected button name from `#toolsearch-set` (map index → `TOOL_SEARCH_MODES[index]`, or pressed button's label/id suffix)
    - advisor: `self._advisor_value_text()` which is `"off"` or a slug

- [ ] **Step 1: Write failing dependency tests**

```python
@pytest.mark.anyio
async def test_lsp_off_dims_and_disables_nav_tools():
    from dataclasses import replace

    env_cfg = _env_cfg()
    # default lsp_enabled is True in ModelConfig — force off
    env_cfg = replace(env_cfg, lsp_enabled=False)
    app = _Host(_fake_harness(), env_cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        row = app.screen.query_one("#row-lsp-tools")
        box = app.screen.query_one("#sw-lsp-tools")
        assert "dimmed" in row.classes
        assert box.disabled is True


@pytest.mark.anyio
async def test_toggling_lsp_enables_nav_tools_live():
    env_cfg = _env_cfg()
    from dataclasses import replace

    env_cfg = replace(env_cfg, lsp_enabled=False)
    app = _Host(_fake_harness(), env_cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        assert app.screen.query_one("#sw-lsp-tools").disabled is True
        _scroll_to(app, "#sw-lsp")
        await pilot.click("#sw-lsp")
        await pilot.pause()
        assert app.screen.query_one("#sw-lsp-tools").disabled is False
        assert "dimmed" not in app.screen.query_one("#row-lsp-tools").classes


@pytest.mark.anyio
async def test_tiering_off_dims_tier_rows():
    from dataclasses import replace

    env_cfg = _env_cfg()
    env_cfg.subagent.tiers = replace(env_cfg.subagent.tiers, enabled=False)
    app = _Host(_fake_harness(), env_cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        for tier in ("cheap", "med", "high"):
            row = app.screen.query_one(f"#row-tier-{tier}")
            btn = app.screen.query_one(f"#tier-change-{tier}")
            assert "dimmed" in row.classes
            assert btn.disabled is True


@pytest.mark.anyio
async def test_advisor_off_dims_token_knobs_and_pick_undims():
    app = _Host(_fake_harness(), _env_cfg())  # advisor_model default None → "off"
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        assert app.screen.query_one("#advisor-max-tokens").disabled is True
        assert app.screen.query_one("#advisor-max-uses").disabled is True
        app.screen._on_advisor_chosen("openrouter/guide")
        await pilot.pause()
        assert app.screen.query_one("#advisor-max-tokens").disabled is False
        assert "dimmed" not in app.screen.query_one("#row-advisor-tokens").classes
```

If `ModelConfig` is not a full dataclass `replace` target for `lsp_enabled`, set attributes directly (`env_cfg.lsp_enabled = False`) the same way other tests mutate `env_cfg.subagent.tiers`.

For the advisor pick test without writing real `.env`, either monkeypatch `XDG_CONFIG_HOME` like other save tests, or monkeypatch `app.screen._env.save` to return `True` and still update `env_cfg` via the real `_on_advisor_chosen` path — prefer the existing `tmp_path` + `XDG_CONFIG_HOME` pattern from `test_tier_choice_saves_env_and_refreshes_catalog`.

Add a toolsearch variant if cheap:

```python
@pytest.mark.anyio
async def test_toolsearch_off_disables_threshold(isolated_env):
    env_cfg = _env_cfg()
    env_cfg.tool_search = "off"
    app = _Host(_fake_harness(), env_cfg)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        assert app.screen.query_one("#toolsearch-threshold").disabled is True
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest --no-cov tests/test_settings_screen.py -k "dims or undims or disables_threshold or enables_nav" -v`

Expected: FAIL (`disabled` still False / no `dimmed` class).

- [ ] **Step 3: Implement `_refresh_dependencies` and call sites**

```python
from .settings_env import (
    ...
    TOOL_SEARCH_MODES,
    dependents_enabled,
)

def _toolsearch_value(self) -> str:
    rs = self.query_one("#toolsearch-set", RadioSet)
    if rs.pressed_button is not None and rs.pressed_button.id:
        # ids are toolsearch-off|auto|on
        return rs.pressed_button.id.removeprefix("toolsearch-")
    if rs.index is not None:
        return TOOL_SEARCH_MODES[rs.index]
    return "off"

def _refresh_dependencies(self) -> None:
    enabled = dependents_enabled(
        {
            "sw-lsp": self.query_one("#sw-lsp", BoxCheckbox).value,
            "sw-tiering": self.query_one("#sw-tiering", BoxCheckbox).value,
        },
        {
            "toolsearch": self._toolsearch_value(),
            "advisor": self._advisor_value_text(),
        },
    )
    for row_id, on in enabled.items():
        row = self.query_one(f"#{row_id}")
        row.set_class(not on, "dimmed")
        for child in row.query("*"):
            if child.focusable or isinstance(child, (BoxCheckbox, Input, Button)):
                child.disabled = not on
```

Call `_refresh_dependencies()`:
- end of `on_mount` (after `_ready = True`, alongside `_refresh_help`)
- after successful env checkbox commit when `cid in {"sw-lsp"}` (in `on_checkbox_changed` after `_env.commit` for that id — simplest: always call `_refresh_dependencies()` at end of checkbox handler once `_ready`, it's cheap)
- end of `_toggle_tiering`
- end of `on_radio_set_changed` when `rid == "toolsearch-set"` (after commit)
- end of `_on_advisor_chosen` after the value Static update

Export `TOOL_SEARCH_MODES` from `settings_env` if not already imported in `settings.py` (it currently lives there; `settings_sections` already imports it).

Keep complexity of `on_checkbox_changed` ≤ 10 — if needed, extract `_handle_env_checkbox(cid, value)` rather than adding branches inline.

- [ ] **Step 4: Run dependency + regression tests — expect PASS**

Run:

```bash
uv run pytest --no-cov \
  tests/test_settings_env_helpers.py \
  tests/test_settings_screen.py \
  -v
```

Expected: all PASS. Especially existing autosave/tiering/workflows tests must still pass (stable control ids).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/settings.py tests/test_settings_screen.py
git commit -m "feat(tui): dim and disable dependent Tools settings controls"
```

---

### Task 5: Lint, types, full verification

**Files:**
- No intentional product changes — fix only issues the gates report.

- [ ] **Step 1: Ruff**

Run: `uv run ruff check src/marim_harness/interfaces/tui/settings.py src/marim_harness/interfaces/tui/settings_sections.py src/marim_harness/interfaces/tui/settings_env.py tests/test_settings_screen.py tests/test_settings_env_helpers.py`

Expected: clean. If import order fails: `uv run ruff check --fix …` then re-check.

- [ ] **Step 2: Pyright**

Run: `uv run pyright src/marim_harness/interfaces/tui/settings.py src/marim_harness/interfaces/tui/settings_sections.py src/marim_harness/interfaces/tui/settings_env.py`

Expected: 0 errors.

- [ ] **Step 3: Full related pytest**

Run: `uv run pytest --no-cov tests/test_settings_env_helpers.py tests/test_settings_screen.py -v`

Expected: all PASS.

- [ ] **Step 4: Manual smoke (optional but recommended)**

Run: `uv run marim` → open settings → Tools. Confirm: groups visible, picker rows not run-on, focusing LSP shows help, LSP off dims nav tools, advisor off dims token rows, escape clears field help to section line, footer status still shows save confirmations.

- [ ] **Step 5: Final commit only if Step 1–3 required fixes**

```bash
git add -u
git commit -m "chore(tui): lint/type fixes for Tools settings polish"
```

If nothing to fix, skip the commit.

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Fix `.srow Static` specificity / picker run-on | Task 2 |
| Six headed groups, no banner/prose walls | Task 2 |
| Compact height-1 numeric inputs (Tools only) | Task 2 |
| Horizontal tool-search RadioSet (Tools only) | Task 2 |
| Shared 24-wide `.row-label` / `.row-value` | Task 2 |
| Session `#model-label` keeps auto width | Task 2 |
| `FIELD_HELP` / `SECTION_HELP` / `help_for` | Task 1, 3 |
| Docked `#settings-help` focus + section behavior | Task 3 |
| Status line stays for save/validation only | Task 3 |
| `CHECK_DEPENDENTS` / `VALUE_DEPENDENTS` / `dependents_enabled` | Task 1, 4 |
| Dim + disable dependents; live refresh | Task 4 |
| Stable control ids / existing autosave | Tasks 2–4 tests |
| Pure helper unit tests + screen tests | Tasks 1, 3, 4 |
| Out of scope: other pages, SettingRow framework, live tier apply | — not planned |

## Placeholder / consistency notes (self-review)

- No TBD/TODO steps; help copy is verbatim from the spec.
- `dependents_enabled` / `help_for` signatures are identical in Tasks 1, 3, and 4.
- Row ids in Task 2 match Task 1 registries exactly.
- Class rename `.tier-row-label` → `.row-label` is called out in Task 2 tests so old selectors cannot silently pass.
