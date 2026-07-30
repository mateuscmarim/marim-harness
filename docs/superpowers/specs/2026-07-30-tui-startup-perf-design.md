# TUI startup performance — early paint + deferred construction — design

**Date:** 2026-07-30
**Status:** Approved (design); implementation plan to follow.

## Goal

Interactive `marim` (TUI) takes ~3.3–3.9s on a cold process before anything
paints. Nearly all of that is **import-time CPU** (not network): `build_harness`
pulls `pydantic_ai` (~2.5s), the MCP stack, tool modules (`markdownify` → `bs4`
→ `lxml`), and `openai` via `build_model`. The user stares at a blank terminal
until the agent graph exists.

**Make the Textual screen appear in ≤ ~0.5s, and cut wall-clock to an
interactive prompt to ≤ ~2.5s**, without changing behavior once ready.

This is the follow-on to [2026-06-19-cli-lazy-imports-design.md](2026-06-19-cli-lazy-imports-design.md),
which deliberately left TUI/headless launch out of scope ("painting the UI
before the agent is built is a separate, larger effort").

## Profiling (measured 2026-07-30, this machine)

Cold process, no network calls during import or `build_harness()` (verified by
monkey-patching `socket.connect` / `getaddrinfo` — **0 calls**). Model catalog
fetch (`list_models`) is async and is **not** on the startup path.

| Phase | Time | Notes |
|-------|------|-------|
| `uv run` overhead | ~0.10s | interpreter resolution |
| router / argparse | ~0.14s | already lazy after June work |
| **bootstrap imports** | **~2.54s** | `pydantic_ai` tree + MCP + tools + compaction |
| **`build_harness()` body** | **~0.90s** | dominated by `pydantic_ai.models.openai` → `openai` SDK (~0.53s) |
| HarnessApp imports | ~0.29–0.41s | Textual widgets, math_markdown |
| **Total to ready** | **~3.3–3.9s** | nothing painted until the end |

Largest cumulative import sinks inside bootstrap:

| Package / module | Cumulative | Role |
|------------------|------------|------|
| `pydantic_ai` | ~2.48s | Agent, capabilities, MCP, template, graph |
| `mcp` + `fastmcp` | ~0.59s | MCP SDK (client, server, OAuth) |
| `logfire_api` | ~0.50s | telemetry pulled by pydantic_ai |
| `tools/provider` → fetch/markdownify/bs4/lxml | ~0.30s | HTML→markdown for `fetch_url` |
| `openai` (via `build_model`) | ~0.53–0.62s | default OpenRouter/OpenAI model path |

**Important nuance:** moving bootstrap's top-level imports into `build_harness()`
alone does **not** reduce TUI wall-clock — the TUI always calls `build_harness`.
Real wins require **skipping work** and/or **painting before that work finishes**.

## Success criteria

| Metric | Today | Target |
|--------|-------|--------|
| Time to first Textual frame | ~3.3–3.9s | **≤ ~0.5s** |
| Time to interactive prompt | ~3.3–3.9s | **≤ ~2.5s** |
| `marim --help` / management commands | already fast (~0.3s) | unchanged |
| Behavior once ready | — | identical (tools, resume, MCP, trust, approvals) |

## Scope

**In scope (focused cut):**

1. **Early paint** — mount the TUI before `build_harness` finishes; construct the
   harness on a Textual worker after first frame.
2. **Lazy tool modules** in `tools/provider.py` so `markdownify`/`bs4`/`lxml`
   stay off the ready path.
3. **Bootstrap import hygiene** — defer heavy top-level imports into
   `build_harness` / `build_lsp_registry` (helps non-TUI importers; not the
   headline TUI metric once early paint lands).
4. **Readiness gate** + fatal construction-failure exit (v1).
5. Regression locks (import invariants, failure path, existing suite green).

**Out of scope (v1):**

- Progressive `HarnessShell` API / multi-stage collaborator attach.
- Lazy TUI submodules (settings, subagents screen, math_markdown) — ~0.3s, separate cut.
- Upstream pydantic_ai / logfire lazy loading.
- Retry UI after construction failure (fatal exit only for v1).
- Headless UX changes (headless keeps sync `build_harness` then run; still
  benefits from import cuts automatically).
- Changing when MCP connects or LSP probes run beyond what falls out of
  existing config gates.

## Approach (chosen)

**Early paint + deferred construction**, plus focused import surgery.

Rejected alternatives:

- **Thin `HarnessShell` + progressive fill** — cleaner long-term, too much API
  surface for the first cut.
- **Import surgery only (no early paint)** — fails the first-paint goal;
  deferring bootstrap imports does not move TUI wall-clock by itself.

### Failure UX (v1)

If harness construction fails after paint: show a clear error, then exit
non-zero. Retry-while-open is a follow-up.

---

## Design

### 1. Startup sequence

**Today:**

```
router → default_cmd → build_harness() [~3s] → HarnessApp(harness).run() → on_mount
```

**Proposed:**

```
router → default_cmd
  → resolve workspace / mode / env model label (light; no pydantic_ai)
  → HarnessApp(TuiLaunch, history).run()     # paints immediately
  → on_mount:
       show banner + status (mode, env model label, "starting…")
       disable prompt
       worker: harness = build_harness(workspace, mode=..., resume=...)
       on success:
         self.harness = harness
         bind_ui(...)                        # moves here from __init__
         existing on_mount tail              # history replay, session_start,
                                             # trust prompt, MCP connect
         enable prompt
       on failure:
         surface error → exit(1)
```

Headless path unchanged: still `build_harness` then `run_headless`.

### 2. Light launch context (`TuiLaunch`)

Only data available without the agent graph:

```python
@dataclass(frozen=True)
class TuiLaunch:
    workspace: Path
    mode: Mode | None          # None → build_harness applies MARIM_DEFAULT_MODE
    resume: bool
    model_label: str           # env-derived, display-only until harness ready
```

- `default_cmd` builds `TuiLaunch` + `PromptHistory`; it does **not** call
  `build_harness` on the TUI path.
- `build_harness` is imported **inside the worker**, so the paint path stays off
  the bootstrap/pydantic_ai import chain.
- `Mode` must remain import-safe (prior lazy-import work). If it regresses and
  pulls `pydantic_ai`, pass mode as `str | None` until the worker and convert
  inside `build_harness`.

### 3. `HarnessApp` readiness

- `self.harness: Harness | None` starts `None`.
- An `asyncio.Future` (or Textual worker result) represents readiness.
- **Before ready:**
  - Prompt input disabled (or submits no-op with a short "still starting…" notice).
  - Slash commands / keybindings that touch `self.harness` wait or no-op safely.
  - Pure UI actions (quit, scroll) work immediately.
- **After ready:** behavior matches today's post-`on_mount` state.
- **`bind_ui` ordering:** today it runs in `__init__`. After this change it runs
  only once the harness exists (post-worker), immediately before the existing
  session_start / MCP / trust tail. Same callbacks as today.
- **Quit mid-build:** cancel the worker in unmount/exit; no orphan threads.
- **Resume:** status may show "resuming…"; history replay stays after harness
  ready (same content as today, slightly later).

### 4. Cutting total work (import surgery)

#### 4a. Lazy tool modules in `tools/provider.py` (primary ready-time win)

Today `provider.py` imports all concern modules at top level so name→fn maps can
hold callables. That pulls `net_tools` → `impl/fetch.py` → `markdownify` →
`bs4` → `lxml` (~300ms) even when `fetch_url` never runs.

**Change:** keep maps as name → `(module, attr)` (or an equivalent lazy proxy).
Resolve the callable once on first registration / first subagent grant via a
single helper used by both the main agent path and subagent grants.

No tool schema or behavior change — only *when* the implementation module loads.

#### 4b. Bootstrap import hygiene (secondary)

Defer heavy top-level imports in `runtime/bootstrap.py` into `build_harness` /
`build_lsp_registry` (`compaction`, hooks, lsp, mcp, session, trust loaders,
etc. that are only used inside those functions).

With early paint this is **not** the TUI headline metric (cost still lands in
the worker), but it keeps `import bootstrap` cheap for tests, `serve`, and
embedders that only need symbols — and matches the June CLI lazy-import spirit.

#### 4c. Explicitly not deferred in v1

| Item | Why |
|------|-----|
| `pydantic_ai` before prompt usable | Still required to build the agent |
| `openai` SDK in `build_model` | Required for default model before first turn |
| Lazy Textual settings/subagents/math | Out of focused scope |
| Skip MCP/LSP construction when disabled | Follow-up if measurement shows free win |

### 5. Files

| File | Change |
|------|--------|
| `interfaces/cli/default_cmd.py` | TUI path builds `TuiLaunch` + history; no pre-run `build_harness`. |
| `interfaces/tui/app.py` | Accept pending launch; paint; worker-build; bind + readiness gate; fatal failure. |
| `tools/provider.py` (+ tiny helper if needed) | Lazy-resolve tool callables. |
| `runtime/bootstrap.py` | Move heavy imports into the functions that use them. |
| `tests/test_cli_startup.py` (extend) + focused new tests | Invariants, readiness, failure exit. |

Optional: a small `interfaces/tui/launch.py` (or similar) if `TuiLaunch` should
not live in `app.py` / `default_cmd.py` — implementer's call; one frozen
dataclass, no new subsystem.

### 6. Testing

1. **Import invariants (subprocess, fresh interpreter)** — same pattern as
   existing `tests/test_cli_startup.py`:
   - `default_cmd` import still must not load `pydantic_ai`.
   - Light TUI launch path / `HarnessApp` import must not load `pydantic_ai` (or,
     if residual Textual-adjacent imports appear, assert the *bootstrap + tools
     provider* chain is absent until the worker runs).
   - `tools.provider` import must not load `markdownify` or `bs4`.

2. **Readiness gate** — before harness is set, prompt submit / agent-facing
   commands do not crash; after ready, existing behavior holds.

3. **Construction failure** — fake builder raises in the worker → app exits
   non-zero.

4. **Regressions** — existing bootstrap / provider / CLI / TUI tests stay green.
   Optional local timed smoke (not a flaky CI gate) documents before/after on
   a developer machine.

### 7. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Race: user types before ready | Disable prompt; no-op / notice until future completes |
| `bind_ui` / session_start order bugs | Post-ready tail mirrors current `on_mount` order exactly |
| Lazy tools break subagent grants | One resolve helper for main + subagent registration |
| `Mode` import pulls `pydantic_ai` | Verify; fall back to `str` mode until worker |
| Quit mid-build leaves work running | Cancel worker on unmount/exit |
| Resume history "flash" | "resuming…" status; replay only after ready |
| Early paint hides slow construction | Status "starting…"; total ready-time still a success metric |

### 8. Build order

1. Lazy `tools/provider` + import-invariant test (pure ready-time win, low risk).
2. Bootstrap import deferral (hygiene + non-TUI importers).
3. `TuiLaunch` + `HarnessApp` early paint + readiness gate + fatal failure path.
4. Measurement pass + full suite (`ruff` → `pyright` → `pytest`).

## Relationship to prior work

- [2026-06-19-cli-lazy-imports-design.md](2026-06-19-cli-lazy-imports-design.md) —
  made `config`/`models`/`--help` skip `pydantic_ai`. This spec is the TUI
  half of that story.
- `perf(cli): keep marim trust off the pydantic_ai import path` and related
  follow-ups already established subprocess import-invariant testing — reuse
  that harness in `tests/test_cli_startup.py`.
