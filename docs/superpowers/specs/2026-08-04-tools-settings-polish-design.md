# Tools settings page polish

**Date:** 2026-08-04
**Status:** Design — approved, pending implementation plan
**Component:** `src/marim_harness/interfaces/tui/settings.py`,
`settings_sections.py`, `settings_env.py`

## Problem

The 2026-06-30 settings redesign broke the old `Config` catch-all into topic
pages, but the Tools page has since re-accreted the same disease: six features
landed after the redesign (dynamic workflows toggle, wake depth cap, tiering
master switch + three tier pickers, advisor block, thinking block), each adding
its own rows and prose. Five concrete problems are visible in the current page:

1. **A CSS specificity bug garbles the picker rows.** The generic rule
   `.srow Static { width: auto }` (class+type) silently beats
   `.tier-row-label { width: 12 }` and `.tier-row-value { width: 1fr }`
   (single class). Labels collapse to natural width (`Advisor` + `off` renders
   as `Advisoroff`), values stop flexing, and `change` buttons drift to
   jagged positions or wrap on long model names.
2. **Sprawl, again.** Fifteen controls in one flat list with no group
   headers — LSP toggles, tool search, job tool, workflows, sub-agent limits,
   tiering, advisor, thinking.
3. **Prose walls.** Three muted explanation paragraphs (tiering 4 lines,
   advisor 4 lines, thinking 3 lines) exist mostly to explain apply semantics;
   they occupy more space than the controls they describe.
4. **Vertical waste.** Every numeric input renders as a 3-row bordered box
   (`.frow { height: 3 }`), and the tool-search RadioSet stacks vertically.
   The page overflows a standard terminal and must scroll.
5. **Mixed apply cues.** "Live" vs "next launch" vs "new sessions" is
   expressed ad hoc — one banner line tries to cover everything, then three
   prose blocks contradict its generality.

## Goals

- Fix the `.srow` specificity bug so picker rows align.
- Reorganize the page into headed groups with one aligned label column.
- Compact all rows to height 1 so the page fits a 40-line terminal.
- Replace prose walls with a docked, focus-driven help line.
- Make dependent controls dim + unfocusable while their master is off.

Non-goals: changing which settings exist, their env vars, or their persistence
semantics; touching other settings pages (the help-line seam is built so they
can adopt it later); the reusable `SettingRow` framework (approach C, deferred);
live application of per-tier model picks (already a documented follow-up in
`_on_tier_chosen`); a Tools rail badge.

## Design

### Page structure & grouping

`tools_widgets` gains a `group_header(text)` helper (muted, accent-tinted,
height 1, top margin except on the first group) and reorganizes into six
groups, ordered tool-ish → model-ish:

1. **Language server** — `LSP`; `LSP navigation tools` indented under it
2. **Tool search** — off/auto/on radio; `Threshold` indented under it
3. **Agent tools** — `Job tool combined`, `Dynamic workflows (run_workflow)`
4. **Sub-agents** — request limit, wake turns, `Model tiering`, three tier
   rows indented under it
5. **Advisor** — advisor picker row; max tokens, max uses/turn indented
6. **Thinking** — thinking picker row

The top banner `Static` and the three prose `Static` blocks are deleted from
the flow (their content moves to the help line). Dependent rows get
`padding-left: 2` so hierarchy reads even before dimming.

Resulting page (sketch):

```
 Tools
 Language server
 [ ] LSP
 [ ]   LSP navigation tools                      (dimmed while LSP off)
 Tool search
 ( ) off  (•) auto  ( ) on
     Threshold            [15]                   (dimmed while off)
 Agent tools
 [x] Job tool combined
 [ ] Dynamic workflows (run_workflow)
 Sub-agents
     Request limit        [50]
     Autonomous wake turns[20]
 [x] Model tiering
     Cheap tier   zen:deepseek-v4-flash-free   [change]
     Med tier     zen:mimo-v2.5-free           [change]
     High tier    zen:mimo-v2.5-free           [change]
 Advisor
     Advisor      off                          [change]
     Max tokens           [2048]               (dimmed while advisor off)
     Max uses/turn        [0]
 Thinking
     Thinking     off                          [change]
─────────────────────────────────────────────────
 <help line: focused field's text, or the section one-liner on the rail>
 ↑↓ section · enter edit · esc back/close · changes save automatically
```

### Row anatomy & CSS fixes

- **Specificity fix.** Scope the auto-width rule to the one row that needs it:
  the Session model row's `Static` gets its own class (e.g. `.model-label`),
  and `.srow Static { width: auto }` is removed. `.tier-row-label` /
  `.tier-row-value` then apply as written.
- **One label column.** Every label+control row uses the existing 24-wide
  `.frow` label column; picker rows (tier/advisor/thinking) adopt it too,
  replacing the 12-wide `.tier-row-label` (rename to a shared `.row-label`
  class). Checkbox rows are unchanged (the checkbox is its own label).
- **Compact numeric inputs.** A `.num` class renders Inputs height-1,
  borderless, fixed ~14 wide, subtle panel background, accent underline on
  focus; `.frow` height becomes 1. Reclaims ~12 rows of vertical space.
- **Horizontal RadioSet.** The tool-search set lays out horizontally
  (`( ) off (•) auto ( ) on`, one line), reclaiming 2 more rows.

### Docked help line

A `#settings-help` `Static` mounts between `#settings-body` and
`#settings-footer` — screen-level chrome, height auto (max 2 lines), muted,
hidden while empty. Content comes from pure-data registries in
`settings_env.py` (same id-keyed-data role as `ENV_CHECKBOXES` et al.):

```python
FIELD_HELP: dict[str, str]          # focusable widget id -> 1–2 line help
SECTION_HELP: dict[str, str]        # section key -> one-liner ("tools" only)
```

- Resolution is a pure `help_for(widget_id) -> str | None` helper: direct hit,
  else walk up (a focused `RadioButton` resolves to its parent `RadioSet`'s
  id), else `None`.
- The screen shows the field's text while a field is focused; when focus
  returns to the rail (nothing focused) it shows `SECTION_HELP[section]`;
  sections without an entry leave the line hidden. The screen listens for
  descendant focus/blur on the content pane.
- The footer status line is untouched: help says *what a field is*, status
  says *what just happened* (save confirmations, validation errors). They
  never fight.

Help copy (final wording for the registry; keyed by focusable widget id):

| id | text |
|---|---|
| `sw-lsp` | Language-server integration (diagnostics on edit). Applies next launch. |
| `sw-lsp-tools` | Six navigation tools (definitions, references, …). Requires LSP. Applies next launch. |
| `toolsearch-set` | Serve MCP/plugin tools via the search_tools tool instead of up-front schemas. 'auto' activates once the tool count passes the threshold. Applies next launch. |
| `toolsearch-threshold` | Tool count at which 'auto' tool search activates. Applies next launch. |
| `sw-job` | One combined job tool instead of separate list/output/wait/cancel tools. Applies next launch. |
| `sw-workflows` | Model-authored Python workflows in a sandbox (run_workflow). Applies live. |
| `subagent-req-limit` | Maximum model requests per sub-agent run. Applies next launch. |
| `wake-depth-cap` | Maximum autonomous turns after a finished job wakes the agent. Applies next launch. |
| `sw-tiering` | Route new spawns to cheap/med/high tier models. Off sends every spawn to the main model; tier picks stay saved. Applies live to new spawns. |
| `tier-change-cheap` / `-med` / `-high` | Model for {cheap/med/high}-tier spawns; unset inherits the main model. Saves to .env — applies to new sessions. |
| `advisor-change` | A model the agent can consult mid-task for strategic guidance. Saves the global default to .env (new sessions); /advisor overrides per session, live. Type 'off' to clear. |
| `advisor-max-tokens` | Token cap on advisor replies. Applies next launch. |
| `advisor-max-uses` | Advisor calls per turn; 0 = unlimited. Applies next launch. |
| `thinking-change` | Reasoning effort (off/minimal/low/medium/high/xhigh). Saves the global default to .env (new sessions); /think overrides per session, live. Unsupported models ignore it. |

`SECTION_HELP["tools"]`: "Env-backed settings — save automatically on change;
apply next launch unless the field says live."

### Dependency dimming

A registry encodes master → dependent rows, with two master kinds:

```python
# checkbox masters: dependent rows enabled iff the checkbox is on
CHECK_DEPENDENTS: dict[str, list[str]] = {
    "sw-lsp":     ["row-lsp-tools"],
    "sw-tiering": ["row-tier-cheap", "row-tier-med", "row-tier-high"],
}
# value masters: enabled iff the predicate holds for the current value
VALUE_DEPENDENTS: dict[str, tuple[list[str], Callable[[str], bool]]] = {
    "toolsearch-set":  (["row-toolsearch-threshold"], lambda v: v != "off"),
    "advisor-change":  (["row-advisor-tokens", "row-advisor-uses"],
                        lambda v: v != "off"),
}
```

- Each dependent row wraps in a `Horizontal(id="row-…")` (new ids; existing
  control ids are unchanged, so every id-keyed handler and registry keeps
  working).
- `_refresh_dependencies()` recomputes every row from current values: set the
  row's `.dimmed` class (muted text) and `disabled` on its controls.
  Textual's `disabled` both dims and removes the widget from the focus chain —
  "dim + unfocusable" in one attribute.
- It runs on mount (a relaunched session with LSP off mounts pre-dimmed) and
  after every master change: the `sw-lsp` / `sw-tiering` checkbox handlers,
  the tool-search radio handler, and `_on_advisor_chosen`.
- Dimming is display-only derived state, never persisted. Disabled controls
  keep their saved value; it reappears on re-enable.
- A disabled row can't take focus, so its `FIELD_HELP` never shows; the
  dependency is documented on the dependent's own (unreachable-by-keyboard but
  visible) dimmed row and discoverable via the master's help text.

## Files touched

| File | Change |
|---|---|
| `settings_sections.py` | `tools_widgets` restructure: `group_header()`, six groups, `row-*` wrappers, `.num` inputs, horizontal RadioSet; prose Statics + banner deleted |
| `settings.py` | CSS: `.srow` specificity fix, `.group-head`, `.num`, `.dimmed`, `#settings-help`, horizontal RadioSet; mount help line; descendant focus/blur → help lookup; `_refresh_dependencies()` + calls from the four master-change paths |
| `settings_env.py` | `FIELD_HELP`, `SECTION_HELP`, `CHECK_DEPENDENTS`, `VALUE_DEPENDENTS`, pure `help_for()` + dependency-evaluation helpers |
| `tests/test_settings_screen.py`, new pure-helper tests | below |

No changes to `EnvAutoSave`, env var names, persistence semantics, or other
settings pages.

## Error handling

Unchanged: `EnvAutoSave` validation messages and save failures stay on the
footer status line; integer validation still rejects blank/invalid input with
the field-specific message, writing nothing. New code is display-only:
`help_for()` returns `None` for unknown ids → the line hides; the
dependency-evaluation helpers are total over their registries. A screen test
asserts every `FIELD_HELP` key and `row-*` id exists in the Tools DOM, so a
registry/DOM drift fails loudly in CI rather than silently hiding help.

## Testing

- **Pure helpers** (`settings_env`): `help_for()` — direct id, radio-button →
  parent-set walk, unknown → `None`; dependency evaluation for both master
  kinds (checkbox on/off, tool-search radio value, advisor `off` vs a model
  slug).
- **Screen** (extend `tests/test_settings_screen.py`, existing `_Host` pilot
  pattern): every registry id exists in the Tools DOM; focusing `sw-lsp`
  shows its help text and escape-to-rail restores the section one-liner;
  unchecking LSP dims + disables `row-lsp-tools` and `Tab` skips it; advisor
  `off` dims its two knobs and picking a model un-dims them; tiering off dims
  the three tier rows. The existing autosave/radio/picker tests must pass
  unmodified — they are the regression net proving no id drift.

## Out of scope (YAGNI)

Migrating other pages to the help line / compact rows; the `SettingRow`
framework; a Tools rail badge; live per-tier model application; any change to
what the settings *do*.
