# Stats ledger & query layer

**Date:** 2026-07-28  
**Status:** Approved design  
**Scope:** Durable usage ledger + pure aggregation API (no TUI)

## Problem

Claude Code’s Stats screen (Overview heatmap/streaks/session totals, Models
tokens-per-day + per-model share) needs a **historical time series** of usage.
Marim today only keeps a **per-session cumulative** `RunUsage` blob on each
session JSON file:

| Field today | Gap for Stats |
| --- | --- |
| `tokens` (session total) | Not daily, not per-turn |
| `model` | Last model only; `/model` overwrites |
| `duration_seconds` | Session wall/active time only |
| `updated` | No created/first-activity day |
| Sessions dir | Per-workspace; no cross-session index |

The status bar / headless summary (`usage_summary`) and sub-agent cards are
live views. Nothing can answer “tokens per day last month” or “current streak”
without a new ledger.

## Goals

1. Append-only **per-turn** usage events, dual-scoped (**per-workspace + global
   rollup**).
2. Pure **query API** that computes every number a future Claude-like Stats TUI
   needs (Overview + Models, date ranges).
3. Single write seam so spend cannot be double-counted or forgotten.
4. Best-effort I/O: ledger failure must never fail a turn.
5. Bare `HarnessBuilder` stays side-effect-free; stats opt in with sessions.

## Non-goals

- Textual Stats screen, heatmap widgets, `/stats` command
- Fun flavor copy (“× Slaughterhouse-Five”)
- Backfill from existing session JSON (optional follow-up)
- Network sync, multi-user, export UI
- Per-request (finer than turn) granularity
- Cost dashboards beyond storing `resolve_cost` per event

## Decisions

| Topic | Choice |
| --- | --- |
| Storage | Dual JSONL (workspace + global), not SQLite |
| Grain | Per turn-round usage delta (each non-zero `add_usage`) |
| Scope | Per-workspace ledger **and** global rollup |
| Day boundary | UTC (`day` = `YYYY-MM-DD` in UTC) |
| Token total | `input_tokens + output_tokens` where `input_tokens` is the provider-inclusive input (cache is a subset, not added again) |
| Aux models | Advisor / compaction titler spend is **out** unless it already folds into `session.usage` (today it does not) |
| Sub-agents | Counted only via existing fold into session usage + `add_usage` (no separate sub-agent events) |
| Heatmap window | ~52 weeks ending `today`; 7d/30d range toggles **summary + models series**, not the year grid |
| Backfill | None in v1 |

## Architecture

```text
Turn / sub-agent paths
        │
        ▼
SessionController.add_usage(delta)     ◄── only place that mutates session.usage
        │
        ├─ self.usage += delta
        └─ stats_recorder.record(delta)  (best-effort no-op when off)
                │
                ▼
         StatsLedger.append(event)
                │
        ┌───────┴────────┐
        ▼                ▼
  stats/<ws>/turns.jsonl   stats/global/turns.jsonl

query.overview(events, range) / query.models(events, range)
        ▲
        │
ledger.iter_turns(scope)  ──►  load_* convenience wrappers
```

### Package layout

```text
src/marim_harness/stats/
  __init__.py      # public re-exports
  types.py         # TurnEvent, Range, Overview, ModelsReport (frozen dataclasses)
  ledger.py        # path resolution, append, iter (I/O)
  query.py         # pure aggregations over Iterable[TurnEvent]
```

## Event schema

One JSON object per line (`v` for forward compatibility):

```json
{
  "v": 1,
  "ts": "2026-07-28T14:02:11.123456+00:00",
  "day": "2026-07-28",
  "session_id": "feat-stats-a1b2",
  "workspace": "marim-harness-9f3c2a1b",
  "model": "anthropic/claude-sonnet-4-6",
  "input_tokens": 12000,
  "output_tokens": 800,
  "cache_read_tokens": 9000,
  "cache_write_tokens": 200,
  "cost_usd": 0.0123,
  "cost_is_exact": true,
  "session_duration_seconds": 3721.5
}
```

| Field | Meaning |
| --- | --- |
| `v` | Schema version; readers skip unknown versions |
| `ts` | UTC ISO-8601 timestamp at append |
| `day` | UTC calendar day (denormalized for filters/heatmap) |
| `session_id` | Owning session |
| `workspace` | Same slug as sessions: `{workspace_root.name}-{sha256[:12]}` — present on **both** copies so global lines are self-describing |
| `model` | Model id at record time (`get_model_id()` / store); `null` if unknown → query bucket as `"unknown"` |
| token fields | **Delta** for this `add_usage` call, not session cumulative |
| `cost_usd` / `cost_is_exact` | From `resolve_cost(delta, model)`; cost may be `null` |
| `session_duration_seconds` | Session’s cumulative active duration **snapshot** at record time |

**Skip append** when `input_tokens + output_tokens == 0` (cache-only or empty
deltas are not recorded).

**Not in v1:** prompt text, tool output, paths, per-request ids, separate provider field, tool-call counts.

## On-disk layout

Sessions live at:

```text
$XDG_DATA_HOME/marim-harness/sessions/<name>-<digest>/<id>.json
```

Stats live as a **sibling** of the sessions root:

```text
$XDG_DATA_HOME/marim-harness/stats/
  global/turns.jsonl
  <name>-<digest>/turns.jsonl
```

When `with_sessions(dir=D)` rehomes sessions, stats rehome relative to that
sessions base (`D` is the directory that contains `<name>-<digest>/` folders).

**Canonical rule** (also expose as `default_stats_base` and optional builder
`stats_dir=` override):

```text
def default_stats_base(sessions_base: Path) -> Path:
    return (
        sessions_base.parent / "stats"
        if sessions_base.name == "sessions"
        else sessions_base / "stats"
    )
```

Examples:

| `sessions_base` | `stats_base` |
| --- | --- |
| `…/marim-harness/sessions` (CLI default) | `…/marim-harness/stats` |
| `./.myapp/sessions` | `./.myapp/stats` |
| `./.myapp/my-sessions` (name ≠ `sessions`) | `./.myapp/my-sessions/stats` |

Dual-write payload is identical (including `workspace`) to both:

- `stats_base / workspace_slug / turns.jsonl`
- `stats_base / "global" / turns.jsonl`

### Write mechanics

- Open append-only, write one JSON line + `\n`, flush.
- `mkdir` parents as needed.
- Never raise to the turn loop; log on failure.
- v1 does not require `fcntl` locking; each record is one complete line. Add
  advisory locking later only if torn writes appear under concurrent
  TUI/headless/serve.

### Read mechanics

- Iterate lines; skip empty, corrupt, or unknown-`v` lines (log + continue).
- Missing optional keys default safely (0 / `None`).

## Write seam: `add_usage`

Today spend is folded with bare `self.session.usage += delta` in:

- `TurnController` success path (`result.usage`)
- `TurnController` failure-bank path (`round_usage`)
- `SubagentRunner` / run driver paths that fold child spend into the parent
  session

**v1 rule:** replace every `session.usage += x` with `session.add_usage(x)`.

```text
SessionController.add_usage(delta: RunUsage) -> None:
    self.usage += delta
    recorder.record(delta)   # no-op if recorder is None / disabled
```

`StatsRecorder.record`:

1. If `input_tokens + output_tokens == 0`: return.
2. Build `TurnEvent` from delta + `resolve_cost` + `get_model_id()` +
   `session_id` + workspace slug + `duration_seconds` + UTC now.
3. `ledger.append(event)` (dual file).

This records **each non-zero bank of usage exactly once**, matching how session
totals work today (including sub-agent fold-ins). Multi-round approval turns may
emit multiple events the same day/session/model; totals and heatmap stay correct;
session counts use distinct `session_id`.

## Query API (pure)

```text
Range = Literal["all", "7d", "30d"]

overview(events, range, *, today: date | None = None) -> Overview
models(events, range, *, today: date | None = None) -> ModelsReport
```

`today` defaults to UTC today; injectable for tests.

### Range filter

`overview` / `models` receive the **full** event iterable for the chosen scope.
They apply the range internally:

- `7d` / `30d`: summary fields use events with `day >= today - (N-1)` (inclusive).
- `all`: summary fields use every event.

**Heatmap is special:** it always aggregates from the full iterable over
`[today - 364, today]` (52×7 days), **ignoring** the 7d/30d filter. That matches
the decision that date cycling changes summary numbers + models series, not the
year grid. Callers must not pre-filter events before calling `overview`.

### `Overview`

| Field | Computation |
| --- | --- |
| `total_tokens` | sum of `input_tokens + output_tokens` over **range-filtered** events |
| `favorite_model` | model with max total tokens (range-filtered); ties → lexicographically smallest id; `None` if empty |
| `sessions` | count of distinct `session_id` (range-filtered) |
| `longest_session` | max over `session_id` of max(`session_duration_seconds` or 0) among range-filtered events; `None` if no durations |
| `active_days` | distinct `day` count with ≥1 range-filtered event |
| `window_days` | `7` / `30` for those ranges; for `all`, `(max_day - min_day).days + 1` over range-filtered events, or `0` if empty |
| `most_active_day` | `day` with max total tokens (range-filtered); ties → latest day; `None` if empty |
| `longest_streak` | longest run of consecutive calendar days with activity in the **range-filtered** set |
| `current_streak` | computed against activity days in the **full** set (not range-clipped): if `today` active → streak ending today; else if yesterday active → streak ending yesterday; else `0`. (A 7d filter must not zero out a 9-day current streak.) |
| `heatmap` | list of `{day, tokens}` for each day in `[today - 364, today]`, from the **full** event set; tokens `0` if none |

`active_days / window_days` is what a future UI shows as `39/40`.

### `ModelsReport`

| Field | Computation |
| --- | --- |
| `series` | for each day in the **range window** (for `all`, from min event day through `today`): `{day, by_model: {model: total_tokens}}` |
| `totals` | per model `{model, input_tokens, output_tokens, total_tokens, share}` sorted by `total_tokens` desc; `share = total / grand_total` (0 if empty) |

Cache token fields remain on events for a future detail row; v1 Models view
matches the screenshot (in/out + share).

### Convenience loaders (thin I/O + query)

```text
load_overview(scope: "workspace" | "global", range, *, workspace_slug=None, stats_base=None) -> Overview
load_models(...) -> ModelsReport
```

## Builder / env wiring

| Entry | Stats |
| --- | --- |
| Bare `HarnessBuilder` | off — no files |
| `with_sessions()` | on by default; ledger under default stats base |
| `with_sessions(dir=…)` | on; stats base derived as above |
| `with_sessions(..., stats=False)` | explicit opt-out |
| `MARIM_STATS=0` | bootstrap kill switch (CLI preset only) |
| Headless / TUI / serve | automatic via shared session path |

`SessionController` holds `stats_recorder: StatsRecorder | None`.

Protocol:

```text
class StatsRecorder(Protocol):
    def record(self, delta: RunUsage) -> None: ...
```

## Edge cases

| Case | Behavior |
| --- | --- |
| Stats dir missing | create on append; failure → log, drop |
| Disk full / read-only | drop event; turn continues |
| Corrupt JSONL line | skip |
| Unknown `v` | skip |
| `model is None` | store `null`; query uses `"unknown"` |
| Zero-token delta | no append |
| Session deleted | ledger **retains** events |
| Workspace moved (new digest) | new workspace file; global keeps old rows under old slug |
| Open session duration | longest session uses latest snapshot (lower bound until next turn) |
| Concurrent processes | interleaved complete lines OK in v1 |
| `claude-cli` | included when usage maps into `RunUsage` and hits `add_usage` |

## Privacy

- No prompts, diffs, file paths, or tool output — only ids, model slugs, token
  ints, optional cost, timestamps.
- Same user/XDG trust boundary as sessions.
- No network upload.
- Global view includes all workspaces for this user on this machine; workspace
  scope remains available for project-only queries.
- Wipe: delete the `stats/` tree.

## Docs

- Add a “Stats ledger” row to `docs/sdk/sessions-and-state.md` state table when
  implementing.
- Module docstrings on `stats/` describe the dual-write and pure query split.

## Testing

**Pure (`query.py`):**

- Empty input
- Single day / multi-day streaks (including gap breaks)
- `current_streak` when today inactive but yesterday active
- Range filters with injected `today`
- Favorite-model tie → lex smaller id
- Most-active-day tie → latest day
- Models share sums to ~1.0
- UTC day boundary assumptions documented via fixtures

**Ledger:**

- Dual append creates both files with identical lines
- Corrupt line skipped on iterate
- Zero-usage not written
- Read-only directory: `record` does not raise

**Hook:**

- `add_usage` increases `session.usage` and appends one event
- Grep/review: no remaining `session.usage +=` outside `add_usage`
- Stats disabled / bare builder: no `stats/` writes

## Success criteria

1. Fixture of N turns across models/days yields correct overview + models vs
   hand-computed expectations.
2. Workspace scope excludes other workspaces’ events; global includes them.
3. Stats off / bare builder creates nothing under `stats/`.
4. Injected ledger failure does not fail a turn.
5. All usage banking goes through `add_usage`.

## Future (out of this design)

- TUI Stats screen (Overview + Models) consuming `load_*`
- Optional `marim stats --json` CLI
- Approximate backfill from session JSON (`source: "backfill"`)
- SQLite swap behind the same query API if JSONL scans ever hurt
- Flavor lines / copy

## Summary for implementers

Build `marim_harness.stats` (types, ledger, pure query), wire
`SessionController.add_usage` + dual JSONL under XDG sibling `stats/`, keep TUI
for a later plan. The query layer is the product contract the future Stats
screen will call.
