# Nested sub-agent hierarchy in the sub-agents screen

Date: 2026-06-30
Status: Approved (design)

## Problem

The sub-agents screen (Ctrl+X) lists only the top-level agent's foreground
spawns. When a sub-agent itself spawns a child (which it can — see "Correction"
below), that child is invisible: it never appears in the list, and inside the
parent's transcript pane it renders as a plain `▸ Spawn Agent · …` tool row
rather than a live card. There is no parent/child representation anywhere.

### Root cause (why children are dropped today)

The rendering path is already ~90% unified. One dispatch core
(`StreamRenderer.dispatch_stream_event`) drives every stream through the
`_StreamSink` abstraction (`stream_render.py`). The base docstring lists the only
four per-scope differences: mount container, run-state/assistant storage, title
bookkeeping, and **whether a tool call gets intercepted (the `spawn_agent`
special case)**.

Item 4 is the sole real asymmetry, and it is incomplete rather than intentional:

- `_TopLevelSink.intercept_tool` claims `spawn_agent` → builds a `SubAgentWidget`
  card, registers it in `tool_widgets`, creates its detail-host pane
  (`ensure_pane`), and appends it to the ordered `subagents` list.
- `_SubAgentSink` does **not** override `intercept_tool`, so it inherits the base
  no-op → a nested spawn becomes a generic `ToolCallWidget` and is never
  registered.

Crucially, the **backend already emits nested streams**. A nested `spawn_agent`
passes its own `ctx.tool_call_id` as the stream id
(`tools/provider.py:696-699`), and `SubagentRunner._make_event_handler` forwards
every grandchild event via `on_subagent_event(child_stream_id, …)`
(`subagents/runner.py:252`). Those events are dropped only because
`on_subagent_event` early-returns when no card is registered for the stream id
(`stream_render.py:678-680`) — and no card exists because `_SubAgentSink` never
intercepted the spawn.

**Therefore this feature is UI-only.** No changes to `subagents/`, `tools/`,
`runtime/`, or `deps`. We stop dropping streams the backend already produces.

## Correction to CLAUDE.md (item "a")

CLAUDE.md:98-99 states: *"`spawn_agent` is never granted to sub-agents, so they
cannot recurse."* This is stale. `SubagentRunner.build` grants a depth-bound
`spawn_agent` whenever `depth + 1 < max_depth` (`subagents/runner.py:327-341`);
depth is enforced in the tool (`tools/provider.py:639`). Default
`max_depth = 3`, so nesting reaches depth 2 (main=0 → child=1 → grandchild=2;
great-grandchild refused). Fix the doc to describe the real behavior.

## Design

### 1. Data model

Add two fields to `SubAgentWidget`:

- `parent_id: str | None` — the spawning card's `stream_id`; `None` for a
  top-level spawn.
- `depth: int` — `0` for top-level, `parent.depth + 1` otherwise.

Both are set **at card creation, in the sink's spawn-claim path**, where the
parent identity is already in hand: `_TopLevelSink` → `parent_id=None, depth=0`;
`_SubAgentSink` reads them off `self._parent` (`parent_id =
self._parent.stream_id`, `depth = self._parent.depth + 1`). Nothing threads
through the backend. The existing depth ceiling caps nesting at 2 levels, so the
tree stays shallow.

### 2. Sink unification (core change)

Factor the `spawn_agent` claim — currently only in
`_TopLevelSink.intercept_tool` — into a shared helper on the base `_StreamSink`,
called by **both** sinks. The claim does, in order:

1. `mount_spawn_widget(args)` → build the `SubAgentWidget` card and append to
   the ordered `subagents` list.
2. Set `stream_id = event.part.tool_call_id`, plus `parent_id`/`depth`
   (per section 1).
3. `tool_widgets[tool_call_id] = card`.
4. `ensure_pane(card)` → create the detail-host pane keyed by the stream id.
5. Break the current tool run (`set_run(None, None)`).
6. `await container.mount(card)` — into the sink's own container.

The only per-scope difference is the mount container, which each sink already
owns: `#log` for top-level, the parent's `SubAgentPane` for a child. So a child
card lands in its parent's transcript pane, while the child's own transcript
streams into the child's own detail-host pane — exactly mirroring the top-level
card/pane split.

Once the child card is registered in `tool_widgets`, the existing
`on_subagent_event` forwarding lights up the child pane with no new wiring.

**Stays top-level-only** (not moved into the shared helper):

- `ask_user` interception — sub-agents are not granted `ask_user`.
- background/detached-spawn handling (`note_detached_spawn`,
  `fill_finished_detached_cards`) — sub-agents cannot background-spawn
  (`tools/provider.py:648`).

So `_TopLevelSink.intercept_tool` keeps its `ask_user` branch and calls the
shared `spawn_agent` helper; `_SubAgentSink` overrides `intercept_tool` to call
only the shared `spawn_agent` helper.

### 3. List tree rendering

- **Ordering:** keep `self.subagents` in insertion order. Add a *pure* helper in
  `subagent_stats.py`: `tree_order(agents) -> list[TreeRow]`, where each
  `TreeRow` carries `(agent, depth, is_last_sibling)`, produced by a depth-first
  walk over `parent_id` links. Orphaned/malformed `parent_id` (parent not in the
  list) is treated as a root so nothing is ever hidden.
- **Consumers:** both `SubAgentList.refresh_rows` and the app's row-index →
  agent/stream-id selection consume the **same** `tree_order` output, so
  selecting a child row shows the child's pane (panes are keyed by `stream_id`).
- **Row cell:** `row_cells(agent, prefix="")` gains a connector/indent prefix
  applied to the `agent` column — `└─ ` for the last sibling, `├─ ` otherwise,
  with two-space indent per depth level — kept within the 28-char column width.

### 4. Summary bar

`subagent_stats.aggregate` already sums `a.tokens` / `cost_of(a)` over the list,
so nested agents are counted automatically once they are in `subagents`. No
change to the function.

**Verification item (not an assumption):** confirm a parent card's `tokens` are
its own run's usage and do *not* already include the child's, so the header
total does not double-count. Assert this in a test; if it turns out parent usage
is cumulative, the fix is to subtract children when aggregating (documented as a
follow-up, not silently ignored).

### 5. Navigation & rendering invariants (unchanged, verified)

- Clicking a child card in the parent pane navigates to the child pane via the
  existing `SubAgentWidget` click → show-pane behavior (same widget, same
  handler as top-level).
- `_stream_hidden` still works: a child's transcript widgets live in the child's
  own `SubAgentPane`, so they are hidden unless that pane is current; the child
  *card* in the parent pane only updates its title line (no markdown reparse).
- `SubAgentDetailHost` stays a flat `ContentSwitcher` (one pane per stream id,
  one visible). No nested panes, no reparenting — the *list* carries the tree,
  the host shows one transcript at a time.

## Testing

- `tree_order` — pure unit tests: correct DFS ordering, `is_last_sibling`
  computation, multi-level nesting, orphaned/missing `parent_id` treated as root,
  empty input.
- `row_cells` with prefix — pure unit tests: connector/indent for root, middle
  sibling, last sibling, depth-2 node; truncation within 28 chars.
- Sink unification — render-level test: a nested `spawn_agent` event produces a
  registered card in `tool_widgets`, a child pane in the detail host, and a row
  in the tree-ordered list under its parent.
- Summary — test that aggregate counts nested agents once and that the token
  total is not double-counted (the section-4 verification).

## Out of scope

- Any backend/runner/provider changes.
- Nested panes or pane reparenting in the detail host.
- Changes to depth ceiling or spawn semantics.
- Unrelated refactoring of the streaming renderer.

## Affected files

- `src/marim_harness/interfaces/tui/stream_render.py` — sink unification,
  `SubAgentWidget` fields, card-creation path.
- `src/marim_harness/interfaces/tui/widgets/subagent_stats.py` — `tree_order`,
  `row_cells` prefix.
- `src/marim_harness/interfaces/tui/widgets/subagent_viewer.py` — consume
  `tree_order` in `refresh_rows`.
- `src/marim_harness/interfaces/tui/widgets/subagents_view.py` /
  app selection glue — route index→agent through `tree_order`.
- `CLAUDE.md` — fix the stale recursion claim.
- `tests/` — unit + render-level tests above.
