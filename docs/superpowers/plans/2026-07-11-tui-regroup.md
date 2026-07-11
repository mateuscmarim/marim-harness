# TUI Regrouping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Behavior-preserving regrouping of `interfaces/tui/`: gather the six
sub-agent UI modules into a `tui/subagents/` subpackage (fixing the
one-character-apart `subagents_viewer`/`subagent_viewer`/`subagents_view`
naming hazard) and the four interaction-panel modules plus their formatter
into a `tui/interactions/` subpackage.

**Architecture:** Pure `git mv` module moves with import fixups — no code
body changes. Public names are re-exported from each new subpackage's
`__init__.py`; the `widgets/__init__.py` barrel stops re-exporting moved
names and callers are updated honestly (no compatibility shims left behind).

**Tech Stack:** Python ≥3.10, Textual, uv, pytest, ruff (line 100), pyright.

## Global Constraints

- **Behavior-preserving.** Module moves + import updates only. No renames of
  classes/functions, no body edits, no docstring rewrites beyond fixing a
  moved module's own stale self-references to old module paths.
- Use `git mv` for every move so history follows renames.
- **No shims:** update every import site (src and tests) to the new path.
- Invariant comments move untouched with their files.
- Branch `refactor/tui-regroup` off current master. Tasks run SEQUENTIALLY
  (they share `app.py`, `widgets/__init__.py`, and some test files).
- Implementers do NOT commit (controller commits after each task review).
- Per task run only the named targeted suites
  (`uv run pytest --no-cov -p no:cacheprovider <files>`); full
  `ruff → pyright → pytest` gates run once in Task 3.
- `uv` for everything; ruff line 100; py310 syntax.
- Textual note: widget CSS selectors and `query_one(...)` use CLASS names,
  not module paths — moves don't affect them. `app.py`'s
  `CSS_PATH = "styles.tcss"` is relative to app.py, which does not move.

---

### Task 1: `tui/subagents/` subpackage

**Files:**
- Create: `src/marim_harness/interfaces/tui/subagents/__init__.py`
- Move (git mv):
  - `interfaces/tui/subagents_viewer.py` → `interfaces/tui/subagents/screen.py`
  - `interfaces/tui/widgets/subagent_viewer.py` → `interfaces/tui/subagents/list.py`
  - `interfaces/tui/widgets/subagents_view.py` → `interfaces/tui/subagents/view.py`
  - `interfaces/tui/widgets/subagent.py` → `interfaces/tui/subagents/card.py`
  - `interfaces/tui/widgets/subagent_detail.py` → `interfaces/tui/subagents/pane.py`
  - `interfaces/tui/widgets/subagent_stats.py` → `interfaces/tui/subagents/stats.py`
- Modify: `interfaces/tui/widgets/__init__.py` (drop moved re-exports),
  every src/test import site found by the Step 1 audit (expected:
  `app.py`, `session_view.py`, `stream_render.py`, plus
  `tests/test_subagent_card.py`, `test_subagent_detail.py`,
  `test_subagent_stats.py`, `test_subagents_screen.py`, `test_widgets.py`,
  and any other hits).

**Interfaces:**
- Consumes: current module layout at the branch point.
- Produces: package `marim_harness.interfaces.tui.subagents` whose
  `__init__.py` re-exports the public names currently exported for these
  modules by `widgets/__init__.py` (e.g. `SubAgentWidget`, `SubAgentList`,
  `SubAgentPane`, `SubAgentDetailHost`, `SubAgentsView`, `SubAgentSummary` —
  copy the exact list from the barrel) plus `SubAgentsViewer` from
  `screen.py`. Task 2 and Task 3 rely on these paths being final.

- [ ] **Step 1: Audit import sites**

```bash
grep -rn "subagents_viewer\|subagent_viewer\|subagents_view\|widgets\.subagent\|widgets import.*SubAgent\|from .widgets.subagent" src tests
grep -rn "SubAgent" src/marim_harness/interfaces/tui/widgets/__init__.py
```
Record every hit — that is the complete fixup checklist. Also read the six
modules' intra-imports (they import each other and `widgets/tool_summary.py`,
`widgets/format.py` etc.) to plan the relative-import depth changes
(`from .tool_summary import X` → `from ..widgets.tool_summary import X`;
sibling moves become `from .pane import X`).

- [ ] **Step 2: git mv the six modules and create `__init__.py`**

The new `__init__.py` carries a two-line docstring (the sub-agents UI:
screen controller, list, full-bleed view, inline card, transcript pane, pure
stats) and re-exports the public names per Interfaces above.

- [ ] **Step 3: Fix imports**

- Inside the moved modules: siblings via `.`, widgets helpers via
  `..widgets.<mod>`, tui-level modules via `..<mod>` (unchanged depth).
- `widgets/__init__.py`: delete the moved re-export lines (the barrel must
  no longer mention SubAgent* names).
- Every audit hit in src/tests: point at
  `marim_harness.interfaces.tui.subagents` (package re-export) or the
  specific submodule (`...subagents.stats` etc.) — prefer the package
  re-export for public classes and the submodule path for private helpers
  (`stats.tree_order`, module-level test targets), mirroring each site's
  current style.
- Re-run the Step 1 grep: zero references to the old paths/names anywhere.

- [ ] **Step 4: Run targeted suites**

Run: `uv run pytest --no-cov -p no:cacheprovider tests/test_subagent_card.py tests/test_subagent_detail.py tests/test_subagent_stats.py tests/test_subagents_screen.py tests/test_widgets.py tests/test_app.py tests/test_app_decomposition.py tests/test_session_view_replay.py tests/test_imports.py`
Expected: all pass, same counts as pre-move (verify with `git stash`+run if unsure).

- [ ] **Step 5: Lint + type-check**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean / 0 errors.

---

### Task 2: `tui/interactions/` subpackage

**Files:**
- Create: `src/marim_harness/interfaces/tui/interactions/__init__.py`
- Move (git mv):
  - `interfaces/tui/interaction_panel.py` → `interfaces/tui/interactions/base.py`
  - `interfaces/tui/ask_user.py` → `interfaces/tui/interactions/ask_user.py`
  - `interfaces/tui/approval.py` → `interfaces/tui/interactions/approval.py`
  - `interfaces/tui/plan_card.py` → `interfaces/tui/interactions/plan_card.py`
  - `interfaces/tui/widgets/ask_user_render.py` → `interfaces/tui/interactions/ask_user_render.py`
- Modify: `interfaces/tui/app.py`, `interfaces/tui/widgets/tools.py`
  (imports `.ask_user_render`), `interfaces/tui/widgets/__init__.py` (drop
  the ask_user_render re-export if present), and every test hit
  (expected: `tests/test_interaction_panel.py`, `test_ask_user_panel.py`,
  `test_plan_card.py`, `test_approval.py`, `test_ask_user_render.py`, plus
  any others the audit finds).

**Interfaces:**
- Consumes: Task 1's final layout.
- Produces: package `marim_harness.interfaces.tui.interactions` re-exporting
  `InteractionPanel` (from `base`), `AskUserPanel`, `ApprovalPanel`,
  `PlanCard` (copy exact public class names from the modules), with
  `ask_user_render` importable as a submodule.

- [ ] **Step 1: Audit import sites**

```bash
grep -rn "interaction_panel\|tui\.ask_user\|tui import ask_user\|from \.ask_user\|from \.approval\|from \.plan_card\|ask_user_render" src tests
```
Watch for false hits: `tui/subagents/` (Task 1's output) and non-tui
`ask_user` modules exist (`marim_harness/ask_user.py` at package root and
the `ask_user` tool) — only `interfaces/tui/` paths are in scope; do NOT
touch `src/marim_harness/ask_user.py` or tool-layer imports of it.

- [ ] **Step 2: git mv the five modules and create `__init__.py`**

Docstring: the inline interaction panels (approval / ask_user / plan card)
sharing the `InteractionPanel` base, plus the pure ask_user transcript
formatter. `base.py`'s internal imports: tui-level modules via `..<mod>`,
widgets via `..widgets.<mod>`; `ask_user.py` imports `.base` and
`.ask_user_render` as siblings.

- [ ] **Step 3: Fix imports**

`app.py` (imports `.approval`, `.ask_user`, `.interaction_panel`,
`.plan_card` per the audit) → `.interactions` package re-exports;
`widgets/tools.py` → `from ..interactions.ask_user_render import ...`;
tests per audit. Re-run the audit grep: zero stale paths.

- [ ] **Step 4: Run targeted suites**

Run: `uv run pytest --no-cov -p no:cacheprovider tests/test_interaction_panel.py tests/test_ask_user_panel.py tests/test_ask_user_render.py tests/test_plan_card.py tests/test_approval.py tests/test_app.py tests/test_app_present_plan.py tests/test_present_plan_tool.py tests/test_widgets.py tests/test_imports.py`
Expected: all pass, same counts.

- [ ] **Step 5: Lint + type-check**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean / 0 errors.

---

### Task 3: Docs and full gates

**Files:**
- Modify: `CLAUDE.md` (the `interfaces/tui/` bullet)

- [ ] **Step 1: Update CLAUDE.md**

Extend the `interfaces/tui/` bullet to mention the two subpackages in its
existing style, e.g.: Textual app, widgets, `styles.tcss`, streaming render;
`subagents/` (the sub-agents screen/list/view/card/pane/stats) and
`interactions/` (the approval / ask-user / plan-card inline panels).

- [ ] **Step 2: Full CI-parity gates**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest -q -p no:cacheprovider`
Expected: ruff clean; pyright 0 errors; full suite green, coverage ≥ 90%.
