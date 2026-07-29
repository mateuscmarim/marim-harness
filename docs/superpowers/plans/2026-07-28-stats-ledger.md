# Stats Ledger & Query Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist per-turn usage into a dual JSONL ledger (per-workspace + global) and expose pure `overview` / `models` query helpers so a future Claude-like Stats TUI can be built without further backend work.

**Architecture:** New package `marim_harness.stats` owns types, append-only JSONL I/O, and pure aggregations. All session spend already banks via `session.usage += delta`; those sites move to `SessionController.add_usage(delta)`, which mutates usage and best-effort-records a `TurnEvent`. Builder `with_sessions()` opts stats on by default (sibling `stats/` of the sessions base); bare builder stays side-effect-free.

**Tech Stack:** Python 3.10+, pydantic-ai `RunUsage`, pytest, existing `resolve_cost` in `usage.py`, XDG paths matching `session/store.py`.

**Spec:** `docs/superpowers/specs/2026-07-28-stats-ledger-design.md` (approved).

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax (`list[str] | None` OK; no `type` statements requiring 3.12).
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pytest`/`pip`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; complexity cap 10 — extract helpers, never `# noqa: C901`.
- pyright standard mode: 0 errors.
- Async tests use `anyio` (`@pytest.mark.anyio`) only if needed; this feature is sync-first.
- No live-model tests. No TUI work in this plan.
- Ledger I/O is **best-effort**: never raise into the turn loop.
- Day boundary is **UTC**.
- Token total = `input_tokens + output_tokens` (input is provider-inclusive; do **not** add cache on top).
- Each task: failing test → fail run → implement → pass run → commit with **explicit** `git add <files>` (never `git add -A` / `git add .`).
- Preserve long "why" comments; write new ones in the same style.

## File Structure

| File | Role |
| --- | --- |
| `src/marim_harness/stats/__init__.py` | Public re-exports |
| `src/marim_harness/stats/types.py` | Frozen dataclasses + `Range` alias |
| `src/marim_harness/stats/query.py` | Pure `overview` / `models` |
| `src/marim_harness/stats/ledger.py` | Paths, append, iterate, `default_stats_base`, `workspace_slug` |
| `src/marim_harness/stats/recorder.py` | `StatsRecorder` / `NullStatsRecorder` / `LedgerStatsRecorder` |
| `src/marim_harness/session/store.py` | Export `workspace_slug` (thin public wrapper of existing hash) |
| `src/marim_harness/session/ctrl.py` | `stats_recorder` + `add_usage` |
| `src/marim_harness/runtime/controller.py` | `usage +=` → `add_usage` |
| `src/marim_harness/subagents/runner.py` | `usage +=` → `add_usage` |
| `src/marim_harness/subagents/run_driver.py` | `usage +=` → `add_usage` + comment updates |
| `src/marim_harness/subagents/backend.py` | Comment update only |
| `src/marim_harness/runtime/harness.py` | Wire `stats_recorder` into `SessionController` |
| `src/marim_harness/runtime/builder.py` | `with_sessions(..., stats=True)`, derive stats base |
| `src/marim_harness/runtime/bootstrap.py` | Honor `MARIM_STATS=0` |
| `.env.example` | Document `MARIM_STATS` |
| `docs/sdk/sessions-and-state.md` | Stats ledger row |
| `tests/test_stats_query.py` | Pure query tests |
| `tests/test_stats_ledger.py` | I/O + recorder tests |
| `tests/test_stats_add_usage.py` | SessionController seam tests |

---

### Task 1: Types + pure query layer

**Files:**
- Create: `src/marim_harness/stats/types.py`
- Create: `src/marim_harness/stats/query.py`
- Create: `src/marim_harness/stats/__init__.py`
- Test: `tests/test_stats_query.py`

**Interfaces:**
- Produces:
  - `Range = Literal["all", "7d", "30d"]`
  - `@dataclass(frozen=True) class TurnEvent` with fields: `v: int`, `ts: str`, `day: str`, `session_id: str`, `workspace: str`, `model: str | None`, `input_tokens: int`, `output_tokens: int`, `cache_read_tokens: int`, `cache_write_tokens: int`, `cost_usd: float | None`, `cost_is_exact: bool`, `session_duration_seconds: float | None`
  - `@dataclass(frozen=True) class HeatmapDay`: `day: str`, `tokens: int`
  - `@dataclass(frozen=True) class Overview`: `total_tokens`, `favorite_model`, `sessions`, `longest_session`, `active_days`, `window_days`, `most_active_day`, `longest_streak`, `current_streak`, `heatmap: list[HeatmapDay]`
  - `@dataclass(frozen=True) class ModelTotal`: `model`, `input_tokens`, `output_tokens`, `total_tokens`, `share`
  - `@dataclass(frozen=True) class DayModelSeries`: `day: str`, `by_model: dict[str, int]`
  - `@dataclass(frozen=True) class ModelsReport`: `series: list[DayModelSeries]`, `totals: list[ModelTotal]`
  - `def overview(events: Iterable[TurnEvent], range: Range, *, today: date | None = None) -> Overview`
  - `def models(events: Iterable[TurnEvent], range: Range, *, today: date | None = None) -> ModelsReport`
  - Helpers (module-private OK): `_event_tokens(e) -> int` = `e.input_tokens + e.output_tokens`; `_model_key(e) -> str` = `e.model or "unknown"`; `_parse_day(s) -> date`

**Spec rules to encode exactly:**
- Range filter for **summary** fields: `7d`/`30d` → `day >= today - (N-1)`; `all` → no filter.
- **Heatmap** always uses full event set over `[today-364, today]` inclusive (365 days = 52×7 + 1? Spec says `[today - 364, today]` which is 365 days). Use `today - timedelta(days=364)` through `today` inclusive.
- **current_streak** uses **full** activity set (not range-clipped).
- **longest_streak** uses **range-filtered** activity days.
- Favorite model ties → lexicographically smallest model id.
- Most-active-day ties → **latest** day.
- `window_days`: 7 / 30 / or `(max_day - min_day).days + 1` for all (0 if empty).
- `longest_session`: max over session_id of max(duration); `None` if no non-None durations.
- `current_streak`: if today in active days → length of streak ending today; elif yesterday in active → streak ending yesterday; else 0.
- Empty events → zeros / empty heatmap (still 365 zero days) / `None` favorites.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stats_query.py`:

```python
"""Pure stats aggregations — no filesystem."""
from __future__ import annotations

from datetime import date

from marim_harness.stats.query import models, overview
from marim_harness.stats.types import TurnEvent


def _e(
    day: str,
    *,
    session_id: str = "s1",
    model: str | None = "opus",
    inp: int = 100,
    out: int = 50,
    dur: float | None = 10.0,
    workspace: str = "ws-aaa",
) -> TurnEvent:
    return TurnEvent(
        v=1,
        ts=f"{day}T12:00:00+00:00",
        day=day,
        session_id=session_id,
        workspace=workspace,
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=None,
        cost_is_exact=False,
        session_duration_seconds=dur,
    )


def test_overview_empty():
    today = date(2026, 7, 28)
    o = overview([], "all", today=today)
    assert o.total_tokens == 0
    assert o.favorite_model is None
    assert o.sessions == 0
    assert o.longest_session is None
    assert o.active_days == 0
    assert o.window_days == 0
    assert o.most_active_day is None
    assert o.longest_streak == 0
    assert o.current_streak == 0
    assert len(o.heatmap) == 365
    assert all(h.tokens == 0 for h in o.heatmap)
    assert o.heatmap[0].day == "2025-07-29"  # today - 364
    assert o.heatmap[-1].day == "2026-07-28"


def test_overview_totals_and_favorite_tie_lex_smaller():
    today = date(2026, 7, 28)
    events = [
        _e("2026-07-28", model="sonnet", inp=100, out=0),
        _e("2026-07-28", model="opus", inp=100, out=0),
    ]
    o = overview(events, "all", today=today)
    assert o.total_tokens == 200
    # tie on tokens → lex smaller id wins
    assert o.favorite_model == "opus"
    assert o.sessions == 1


def test_most_active_day_tie_picks_latest():
    today = date(2026, 7, 28)
    events = [
        _e("2026-07-26", inp=100, out=0),
        _e("2026-07-27", inp=100, out=0),
    ]
    o = overview(events, "all", today=today)
    assert o.most_active_day == "2026-07-27"


def test_longest_and_current_streak():
    today = date(2026, 7, 28)
    # active: 25,26,27 and 28 — current streak 4; gap before 25
    events = [
        _e("2026-07-20"),
        _e("2026-07-25"),
        _e("2026-07-26"),
        _e("2026-07-27"),
        _e("2026-07-28"),
    ]
    o = overview(events, "all", today=today)
    assert o.longest_streak == 4
    assert o.current_streak == 4
    assert o.active_days == 5


def test_current_streak_uses_yesterday_when_today_inactive():
    today = date(2026, 7, 28)
    events = [_e("2026-07-26"), _e("2026-07-27")]
    o = overview(events, "all", today=today)
    assert o.current_streak == 2


def test_current_streak_ignores_7d_clip():
    """A 9-day streak must survive range='7d' for current_streak."""
    today = date(2026, 7, 28)
    days = [f"2026-07-{d:02d}" for d in range(20, 29)]  # 20..28 = 9 days
    events = [_e(d) for d in days]
    o = overview(events, "7d", today=today)
    assert o.current_streak == 9
    # summary active_days is range-clipped (7d window starts 2026-07-22)
    assert o.active_days == 7
    assert o.window_days == 7


def test_heatmap_ignores_range_filter():
    today = date(2026, 7, 28)
    events = [_e("2026-06-01", inp=500, out=0), _e("2026-07-28", inp=10, out=0)]
    o = overview(events, "7d", today=today)
    # June 1 is outside 7d summary but inside heatmap year
    by_day = {h.day: h.tokens for h in o.heatmap}
    assert by_day["2026-06-01"] == 500
    assert o.total_tokens == 10  # 7d summary only has July 28


def test_longest_session_per_session_max_snapshot():
    today = date(2026, 7, 28)
    events = [
        _e("2026-07-28", session_id="a", dur=100.0),
        _e("2026-07-28", session_id="a", dur=250.0),
        _e("2026-07-28", session_id="b", dur=200.0),
    ]
    o = overview(events, "all", today=today)
    assert o.longest_session == 250.0


def test_models_share_and_series():
    today = date(2026, 7, 28)
    events = [
        _e("2026-07-27", model="opus", inp=70, out=30),
        _e("2026-07-28", model="sonnet", inp=40, out=10),
        _e("2026-07-28", model="opus", inp=10, out=0),
    ]
    r = models(events, "all", today=today)
    assert r.totals[0].model == "opus"
    assert r.totals[0].total_tokens == 110
    assert r.totals[0].input_tokens == 80
    assert r.totals[0].output_tokens == 30
    assert abs(r.totals[0].share - 110 / 160) < 1e-9
    assert r.totals[1].model == "sonnet"
    assert abs(sum(t.share for t in r.totals) - 1.0) < 1e-9
    # series covers min day .. today
    assert r.series[0].day == "2026-07-27"
    assert r.series[-1].day == "2026-07-28"
    last = r.series[-1].by_model
    assert last == {"sonnet": 50, "opus": 10}


def test_unknown_model_bucket():
    today = date(2026, 7, 28)
    o = overview([_e("2026-07-28", model=None)], "all", today=today)
    assert o.favorite_model == "unknown"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest --no-cov -n 0 tests/test_stats_query.py -v
```

Expected: FAIL (import error / module missing).

- [ ] **Step 3: Implement types + query**

`src/marim_harness/stats/types.py` — frozen dataclasses as listed in Interfaces.

`src/marim_harness/stats/query.py` — pure functions. Keep complexity ≤10 by extracting:

```python
def _utc_today() -> date: ...
def _filter_range(events, range, today) -> list[TurnEvent]: ...
def _tokens(e: TurnEvent) -> int: return e.input_tokens + e.output_tokens
def _model_key(e: TurnEvent) -> str: return e.model or "unknown"
def _active_days(events) -> set[date]: ...
def _streak_ending(active: set[date], end: date) -> int: ...
def _longest_streak(active: set[date]) -> int: ...
def _heatmap(events, today) -> list[HeatmapDay]: ...
```

`current_streak` algorithm:

```python
active = _active_days(all_events)  # full set
if today in active:
    current = _streak_ending(active, today)
elif (today - timedelta(days=1)) in active:
    current = _streak_ending(active, today - timedelta(days=1))
else:
    current = 0
```

`_streak_ending`: count backward while `end - i` in active.

`src/marim_harness/stats/__init__.py`:

```python
"""Durable usage ledger + pure stats queries (no TUI)."""
from .query import models, overview
from .types import (
    DayModelSeries,
    HeatmapDay,
    ModelTotal,
    ModelsReport,
    Overview,
    Range,
    TurnEvent,
)

__all__ = [
    "DayModelSeries",
    "HeatmapDay",
    "ModelTotal",
    "ModelsReport",
    "Overview",
    "Range",
    "TurnEvent",
    "models",
    "overview",
]
```

(Leave ledger/recorder exports for later tasks.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest --no-cov -n 0 tests/test_stats_query.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/stats/__init__.py \
        src/marim_harness/stats/types.py \
        src/marim_harness/stats/query.py \
        tests/test_stats_query.py
git commit -m "feat(stats): pure overview/models query layer"
```

---

### Task 2: Ledger paths + JSONL I/O

**Files:**
- Create: `src/marim_harness/stats/ledger.py`
- Modify: `src/marim_harness/session/store.py` (export `workspace_slug`)
- Modify: `src/marim_harness/stats/__init__.py` (re-export path helpers + loaders later)
- Test: `tests/test_stats_ledger.py`

**Interfaces:**
- Consumes: `TurnEvent` from types; `dataclasses.asdict` or manual dict for JSON.
- Produces:
  - `def workspace_slug(workspace_root: Path | str) -> str`  
    Same as sessions: `f"{Path(workspace_root).name}-{sha256(str(resolved))[:12]}"`  
    Prefer implementing once in `session/store.py` as public `workspace_slug()` used by `_workspace_dir`, and re-export/import from stats.
  - `def default_stats_base(sessions_base: Path) -> Path`  
    `sessions_base.parent / "stats"` if `sessions_base.name == "sessions"` else `sessions_base / "stats"`.
  - `def default_sessions_base() -> Path` — mirror store’s XDG `…/marim-harness/sessions` (or import `_default_base_dir` by promoting it to public `default_sessions_base` in store.py).
  - `class StatsLedger:`
    - `__init__(self, stats_base: Path, workspace_slug: str)`
    - `@property workspace_path -> Path` → `stats_base / workspace_slug / "turns.jsonl"`
    - `@property global_path -> Path` → `stats_base / "global" / "turns.jsonl"`
    - `def append(self, event: TurnEvent) -> None` — dual-write; never raises
    - `def iter_workspace(self) -> Iterator[TurnEvent]`
    - `def iter_global(self) -> Iterator[TurnEvent]`
  - Module functions:
    - `def iter_turns(path: Path) -> Iterator[TurnEvent]` — shared parser
    - `def event_from_dict(data: dict) -> TurnEvent | None` — unknown `v` → None; corrupt → None
    - `def event_to_dict(event: TurnEvent) -> dict`

**Write mechanics:**
- `mkdir(parents=True, exist_ok=True)` on parent of each file.
- Open `"a"`, `write(json.dumps(event_to_dict(event), separators=(",", ":")) + "\n")`, `flush`.
- Catch `OSError` / `TypeError` per file; log with `logging.getLogger(__name__)`; do not raise.
- Dual-write: attempt workspace file then global file independently (one failure must not skip the other if the other can succeed).

**Read mechanics:**
- Skip empty lines, JSON errors, non-dict, `v != 1`, missing required keys (use `.get` with defaults for optional: tokens 0, cost None, duration None, model None).
- Required to accept: at least `day`, `session_id` (if missing session_id, skip line).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stats_ledger.py`:

```python
"""Stats JSONL ledger I/O."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from marim_harness.stats.ledger import (
    StatsLedger,
    default_stats_base,
    event_from_dict,
    iter_turns,
    workspace_slug,
)
from marim_harness.stats.types import TurnEvent


def _event(**over) -> TurnEvent:
    base = dict(
        v=1,
        ts="2026-07-28T12:00:00+00:00",
        day="2026-07-28",
        session_id="s1",
        workspace="proj-deadbeefcafe",
        model="opus",
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.01,
        cost_is_exact=True,
        session_duration_seconds=3.5,
    )
    base.update(over)
    return TurnEvent(**base)


def test_default_stats_base_sibling_of_sessions(tmp_path: Path):
    assert default_stats_base(tmp_path / "sessions") == tmp_path / "stats"
    assert default_stats_base(tmp_path / "my-sessions") == tmp_path / "my-sessions" / "stats"


def test_workspace_slug_stable(tmp_path: Path):
    root = tmp_path / "marim-harness"
    root.mkdir()
    a = workspace_slug(root)
    b = workspace_slug(root)
    assert a == b
    assert a.startswith("marim-harness-")
    assert len(a.rsplit("-", 1)[-1]) == 12


def test_append_dual_write(tmp_path: Path):
    slug = "ws-abc"
    ledger = StatsLedger(tmp_path, slug)
    ev = _event(workspace=slug)
    ledger.append(ev)
    assert ledger.workspace_path.exists()
    assert ledger.global_path.exists()
    w_lines = ledger.workspace_path.read_text().strip().splitlines()
    g_lines = ledger.global_path.read_text().strip().splitlines()
    assert len(w_lines) == 1 and w_lines == g_lines
    data = json.loads(w_lines[0])
    assert data["session_id"] == "s1"
    assert data["input_tokens"] == 10
    assert data["workspace"] == slug


def test_iter_skips_corrupt_and_unknown_v(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    good = _event()
    from marim_harness.stats.ledger import event_to_dict
    lines = [
        "not-json",
        json.dumps({**event_to_dict(good), "v": 99}),
        json.dumps(event_to_dict(good)),
        "",
    ]
    path.write_text("\n".join(lines) + "\n")
    got = list(iter_turns(path))
    assert len(got) == 1
    assert got[0].session_id == "s1"


def test_append_does_not_raise_on_readonly(tmp_path: Path, monkeypatch):
    ledger = StatsLedger(tmp_path / "nope", "ws")
    # Force open to fail
    def boom(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr("builtins.open", boom)
    ledger.append(_event())  # must not raise


def test_event_from_dict_defaults():
    ev = event_from_dict({
        "v": 1,
        "ts": "t",
        "day": "2026-07-28",
        "session_id": "s",
        "workspace": "w",
    })
    assert ev is not None
    assert ev.input_tokens == 0
    assert ev.model is None
    assert ev.cost_usd is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest --no-cov -n 0 tests/test_stats_ledger.py -v
```

Expected: FAIL (missing ledger module / `workspace_slug`).

- [ ] **Step 3: Implement store slug + ledger**

In `src/marim_harness/session/store.py`, promote the hash helper:

```python
def workspace_slug(workspace_root: Path | str) -> str:
    """Stable per-workspace directory name: ``{name}-{sha256[:12]}``."""
    root = Path(workspace_root).resolve()
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return f"{root.name}-{digest}"


def _workspace_dir(base: Path, workspace_root: Path) -> Path:
    return Path(base) / workspace_slug(workspace_root)
```

Also rename or alias `_default_base_dir` → public `default_sessions_base` (keep `_default_base_dir = default_sessions_base` if many internal refs).

Implement `ledger.py` per Interfaces. Import `workspace_slug` from `..session.store` (or re-export a thin wrapper in ledger that calls store — avoid duplicating the hash).

If importing `session.store` from `stats.ledger` risks cycles: put `workspace_slug` + `default_sessions_base` in a tiny leaf (e.g. keep them in `store.py` — stats → session.store is fine; session must not import stats).

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest --no-cov -n 0 tests/test_stats_ledger.py tests/test_stats_query.py -v
```

Expected: PASS. Also run any existing session tests that might touch `_workspace_dir`:

```bash
uv run pytest --no-cov -n 0 tests/test_agent_sessions.py -v --tb=no -q
```

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/stats/ledger.py \
        src/marim_harness/session/store.py \
        tests/test_stats_ledger.py
git commit -m "feat(stats): dual JSONL ledger append and iterate"
```

---

### Task 3: StatsRecorder + load_* helpers

**Files:**
- Create: `src/marim_harness/stats/recorder.py`
- Modify: `src/marim_harness/stats/__init__.py`
- Modify: `src/marim_harness/stats/ledger.py` (add `load_overview` / `load_models` here or in a thin `load.py` — prefer `ledger.py` bottom or `recorder.py`; **put loaders in `ledger.py`** next to iter)
- Test: extend `tests/test_stats_ledger.py`

**Interfaces:**
- Produces:
  - `class StatsRecorder(Protocol):`
    - `def record(self, delta: RunUsage) -> None: ...`
  - `class NullStatsRecorder:` with no-op `record`
  - `class LedgerStatsRecorder:`
    - `__init__(self, ledger: StatsLedger, *, session_id: str, get_model_id: Callable[[], str | None], get_duration_seconds: Callable[[], float | None])`
    - `record(self, delta: RunUsage) -> None`:
      1. If `(delta.input_tokens or 0) + (delta.output_tokens or 0) == 0`: return
      2. `cost, exact = resolve_cost(delta, get_model_id())`
      3. Build `TurnEvent` with `v=1`, UTC `ts`/`day`, `workspace=ledger`’s slug, token fields from delta (use `or 0`), `session_duration_seconds=get_duration_seconds()`
      4. `ledger.append(event)` inside try/except Exception log (belt-and-suspenders)
  - `def load_overview(scope: Literal["workspace","global"], range: Range, *, stats_base: Path, workspace_slug: str | None = None, today: date | None = None) -> Overview`
  - `def load_models(...)` same signature → `ModelsReport`  
    For `scope=="workspace"`, `workspace_slug` is required (raise `ValueError` if missing). Read the corresponding JSONL via `iter_turns`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_stats_ledger.py`:

```python
from pydantic_ai.usage import RunUsage
from marim_harness.stats.ledger import StatsLedger, load_overview, load_models
from marim_harness.stats.recorder import LedgerStatsRecorder, NullStatsRecorder
from marim_harness.usage import COST_DETAIL_KEY


def test_null_recorder_noop():
    NullStatsRecorder().record(RunUsage(input_tokens=1, output_tokens=1))


def test_ledger_recorder_skips_zero_and_writes_nonzero(tmp_path: Path):
    ledger = StatsLedger(tmp_path, "ws-x")
    rec = LedgerStatsRecorder(
        ledger,
        session_id="sess-1",
        get_model_id=lambda: "anthropic/claude-sonnet-4-6",
        get_duration_seconds=lambda: 42.0,
    )
    rec.record(RunUsage())  # zero
    assert not ledger.workspace_path.exists()
    rec.record(RunUsage(
        input_tokens=100, output_tokens=20,
        cache_read_tokens=10, cache_write_tokens=5,
        details={COST_DETAIL_KEY: 1_000_000},  # $1.00 exact
    ))
    events = list(ledger.iter_workspace())
    assert len(events) == 1
    e = events[0]
    assert e.session_id == "sess-1"
    assert e.model == "anthropic/claude-sonnet-4-6"
    assert e.input_tokens == 100
    assert e.output_tokens == 20
    assert e.cache_read_tokens == 10
    assert e.session_duration_seconds == 42.0
    assert e.cost_usd == 1.0
    assert e.cost_is_exact is True
    assert e.day  # non-empty YYYY-MM-DD


def test_load_overview_workspace_and_global(tmp_path: Path):
    from datetime import date
    from marim_harness.stats.types import TurnEvent
    from marim_harness.stats.ledger import event_to_dict
    import json

    def write(path: Path, *events: TurnEvent):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(event_to_dict(e)) + "\n" for e in events))

    e1 = _event(workspace="ws-a", session_id="s1", input_tokens=50, output_tokens=0)
    e2 = _event(workspace="ws-b", session_id="s2", input_tokens=70, output_tokens=0)
    write(tmp_path / "ws-a" / "turns.jsonl", e1)
    write(tmp_path / "global" / "turns.jsonl", e1, e2)
    today = date(2026, 7, 28)
    ow = load_overview("workspace", "all", stats_base=tmp_path, workspace_slug="ws-a", today=today)
    assert ow.total_tokens == 50
    assert ow.sessions == 1
    og = load_overview("global", "all", stats_base=tmp_path, today=today)
    assert og.total_tokens == 120
    assert og.sessions == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest --no-cov -n 0 tests/test_stats_ledger.py -v
```

Expected: FAIL on missing recorder/loaders.

- [ ] **Step 3: Implement recorder + loaders**

`recorder.py`:

```python
from __future__ import annotations
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from pydantic_ai.usage import RunUsage

from ..usage import resolve_cost
from .ledger import StatsLedger
from .types import TurnEvent

logger = logging.getLogger(__name__)


class StatsRecorder(Protocol):
    def record(self, delta: RunUsage) -> None: ...


class NullStatsRecorder:
    def record(self, delta: RunUsage) -> None:
        return


class LedgerStatsRecorder:
    def __init__(
        self,
        ledger: StatsLedger,
        *,
        session_id: str,
        get_model_id: Callable[[], str | None],
        get_duration_seconds: Callable[[], float | None],
    ) -> None:
        self._ledger = ledger
        self._session_id = session_id
        self._get_model_id = get_model_id
        self._get_duration = get_duration_seconds

    def record(self, delta: RunUsage) -> None:
        try:
            inp = int(delta.input_tokens or 0)
            out = int(delta.output_tokens or 0)
            if inp + out == 0:
                return
            model = self._get_model_id()
            cost, exact = resolve_cost(delta, model)
            now = datetime.now(timezone.utc)
            event = TurnEvent(
                v=1,
                ts=now.isoformat(),
                day=now.date().isoformat(),
                session_id=self._session_id,
                workspace=self._ledger.workspace_slug,  # expose on StatsLedger
                model=model,
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=int(delta.cache_read_tokens or 0),
                cache_write_tokens=int(delta.cache_write_tokens or 0),
                cost_usd=cost,
                cost_is_exact=bool(exact),
                session_duration_seconds=self._get_duration(),
            )
            self._ledger.append(event)
        except Exception:
            logger.exception("stats recorder failed; dropping event")
```

Expose `StatsLedger.workspace_slug` as an attribute set in `__init__`.

Loaders at bottom of `ledger.py` (import query there to avoid cycles — query must not import ledger).

Update `__init__.py` re-exports: `StatsLedger`, `LedgerStatsRecorder`, `NullStatsRecorder`, `load_overview`, `load_models`, `default_stats_base`, `workspace_slug`.

- [ ] **Step 4: Run tests**

```bash
uv run pytest --no-cov -n 0 tests/test_stats_ledger.py tests/test_stats_query.py -v
uv run ruff check src/marim_harness/stats tests/test_stats_ledger.py tests/test_stats_query.py
```

Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/stats/recorder.py \
        src/marim_harness/stats/ledger.py \
        src/marim_harness/stats/__init__.py \
        tests/test_stats_ledger.py
git commit -m "feat(stats): LedgerStatsRecorder and load_overview/models"
```

---

### Task 4: `SessionController.add_usage` seam

**Files:**
- Modify: `src/marim_harness/session/ctrl.py`
- Modify: `src/marim_harness/runtime/controller.py` (2 sites)
- Modify: `src/marim_harness/subagents/runner.py` (1 site)
- Modify: `src/marim_harness/subagents/run_driver.py` (1 site + comments)
- Modify: `src/marim_harness/subagents/backend.py` (comment only)
- Test: `tests/test_stats_add_usage.py`

**Interfaces:**
- Consumes: `StatsRecorder | None`
- Produces on `SessionController`:
  - `__init__(..., stats_recorder: StatsRecorder | None = None)`
  - `self.stats_recorder = stats_recorder`
  - `def add_usage(self, delta: RunUsage) -> None:`
    ```python
    def add_usage(self, delta: RunUsage) -> None:
        """Bank ``delta`` into the session total and best-effort record it
        in the stats ledger. Every call site that used to do
        ``session.usage += x`` must go through here so spend cannot be
        double-counted or forgotten by the ledger."""
        self.usage += delta
        rec = self.stats_recorder
        if rec is not None:
            try:
                rec.record(delta)
            except Exception:
                # Recorder implementations already swallow I/O errors; this
                # guard keeps a buggy recorder from aborting a turn.
                logger.exception("stats_recorder.record failed")
    ```
  - Duration callback used when wiring recorder (Task 5) should match persist’s notion:
    ```python
    def _duration_snapshot(self) -> float:
        elapsed = (time.monotonic() - self._segment_start) if self._segment_start else 0.0
        return self.duration_seconds + elapsed
    ```
    Add this method on `SessionController` in this task (used by harness wiring next).

**Call site replacements (exact):**

| File | Old | New |
| --- | --- | --- |
| `runtime/controller.py` ~703 | `self.session.usage += round_usage` | `self.session.add_usage(round_usage)` |
| `runtime/controller.py` ~963 | `self.session.usage += result.usage` | `self.session.add_usage(result.usage)` |
| `subagents/runner.py` ~564 | `self.session.usage += run.usage` | `self.session.add_usage(run.usage)` |
| `subagents/run_driver.py` ~215 | `self.session.usage += run_usage` | `self.session.add_usage(run_usage)` |

Update comments that mention `session.usage +=` to say `session.add_usage`.

After edits, enforce:

```bash
rg -n "session\.usage\s*\+=" src/marim_harness
```

Expected: **no matches** (comments may still mention the old form in historical notes — prefer rewording those comments).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_stats_add_usage.py
from __future__ import annotations

from pydantic_ai.usage import RunUsage

from marim_harness.session.ctrl import SessionController
from marim_harness.runtime.deps import Deps, WorkspaceConfig
from pathlib import Path


class _FakeRecorder:
    def __init__(self) -> None:
        self.seen: list[RunUsage] = []

    def record(self, delta: RunUsage) -> None:
        self.seen.append(delta)


def _session(tmp_path: Path, rec=None) -> SessionController:
    # Mirror the lightweight construction used across session unit tests:
    # Deps only needs WorkspaceConfig(root=...); other fields default.
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path))
    return SessionController(
        store=None, manager=None, deps=deps,
        max_context_tokens=100_000, keep_last_messages=10,
        stats_recorder=rec,
    )


def test_add_usage_banks_and_records(tmp_path: Path):
    rec = _FakeRecorder()
    s = _session(tmp_path, rec)
    s.add_usage(RunUsage(input_tokens=3, output_tokens=4))
    assert s.usage.input_tokens == 3
    assert s.usage.output_tokens == 4
    assert len(rec.seen) == 1
    assert rec.seen[0].input_tokens == 3


def test_add_usage_without_recorder(tmp_path: Path):
    s = _session(tmp_path, None)
    s.add_usage(RunUsage(input_tokens=1, output_tokens=2))
    assert s.usage.total_tokens == 3


def test_add_usage_recorder_error_does_not_raise(tmp_path: Path):
    class Boom:
        def record(self, delta):
            raise RuntimeError("boom")
    s = _session(tmp_path, Boom())
    s.add_usage(RunUsage(input_tokens=1, output_tokens=0))  # must not raise
    assert s.usage.input_tokens == 1


def test_duration_snapshot_includes_open_segment(tmp_path: Path, monkeypatch):
    s = _session(tmp_path)
    s.duration_seconds = 10.0
    s._segment_start = 100.0
    monkeypatch.setattr("marim_harness.session.ctrl.time.monotonic", lambda: 105.0)
    assert s.duration_snapshot() == 15.0
```

Check real `Deps` / `WorkspaceConfig` constructors in `runtime/deps.py` and adjust the test helper if fields differ — do **not** guess wrong; read the dataclass before writing the test.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest --no-cov -n 0 tests/test_stats_add_usage.py -v
```

Expected: FAIL (`stats_recorder` / `add_usage` missing).

- [ ] **Step 3: Implement `add_usage` + migrate call sites**

1. Add `stats_recorder` param + `add_usage` + `duration_snapshot` on `SessionController`.
2. Replace the four `usage +=` sites.
3. Fix comments in `run_driver.py` / `backend.py`.

Method name in spec discussion was `_duration_snapshot`; public name **`duration_snapshot`** (no leading underscore) so the harness/recorder wiring can call it cleanly.

- [ ] **Step 4: Run tests**

```bash
uv run pytest --no-cov -n 0 tests/test_stats_add_usage.py tests/test_stats_ledger.py tests/test_stats_query.py -v
rg -n "session\.usage\s*\+=" src/marim_harness || true
# Also catch bare self.usage += outside add_usage in controller paths:
rg -n "\.usage\s*\+=" src/marim_harness -g '*.py'
```

Expected: tests PASS; the only remaining `usage +=` is **inside** `add_usage` itself (`self.usage += delta`).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/session/ctrl.py \
        src/marim_harness/runtime/controller.py \
        src/marim_harness/subagents/runner.py \
        src/marim_harness/subagents/run_driver.py \
        src/marim_harness/subagents/backend.py \
        tests/test_stats_add_usage.py
git commit -m "feat(stats): route all usage banking through add_usage"
```

---

### Task 5: Builder + harness wiring + `MARIM_STATS`

**Files:**
- Modify: `src/marim_harness/runtime/builder.py`
- Modify: `src/marim_harness/runtime/harness.py` (`HarnessConfig` + `build_collaborators`)
- Modify: `src/marim_harness/runtime/bootstrap.py`
- Modify: `.env.example`
- Test: `tests/test_stats_wiring.py` (and extend `tests/test_builder.py` if that’s where `with_sessions` is covered)

**Interfaces:**
- `HarnessBuilder.with_sessions(self, dir: Path | None = None, *, stats: bool = True) -> HarnessBuilder`
  - Store `self._stats_enabled = stats` (default True when sessions on).
  - Optional: `stats_dir: Path | None = None` kw-only for explicit override; store as `self._stats_dir`.
- On `build()` / `_open_sessions`: when sessions open successfully and stats enabled, construct:
  ```python
  sessions_base = self._sessions_dir or default_sessions_base()
  stats_base = self._stats_dir or default_stats_base(sessions_base)
  slug = workspace_slug(self._workspace)
  ledger = StatsLedger(stats_base, slug)
  # recorder needs session_id after store.create() — wire in build_collaborators
  ```
  Pass `stats_base` + `workspace_slug` (or a prebuilt ledger) via `HarnessConfig`.

- `HarnessConfig` new optional fields:
  - `stats_ledger: StatsLedger | None = None`
  - or `stats_enabled: bool = False` + `stats_base: Path | None = None`  
  **Prefer:** `stats_ledger: StatsLedger | None = None` built in the builder when sessions+stats on.

- In `build_collaborators`, when constructing `SessionController`:
  ```python
  stats_recorder = None
  if cfg.stats_ledger is not None and cfg.store is not None:
      from ..stats.recorder import LedgerStatsRecorder
      session_id = cfg.store.session_id
      # duration_snapshot needs the controller — chicken/egg:
      # construct SessionController with recorder=None first, then assign, OR
      # use a late-binding closure.
  ```

  **Recommended pattern (late-binding list cell):**

  ```python
  session_holder: list[SessionController] = []
  recorder = None
  if cfg.stats_ledger is not None and cfg.store is not None:
      from ..stats.recorder import LedgerStatsRecorder
      recorder = LedgerStatsRecorder(
          cfg.stats_ledger,
          session_id=cfg.store.session_id,
          get_model_id=get_model_id,
          get_duration_seconds=lambda: (
              session_holder[0].duration_snapshot() if session_holder else None
          ),
      )
  session = SessionController(..., get_model_id=get_model_id, stats_recorder=recorder)
  session_holder.append(session)
  ```

  When the live session **switches** (`switch` / new session), update `LedgerStatsRecorder`’s `session_id` OR rebuild the recorder. Check how model switches work — if `SessionController` is reused with a new `store`, add `session.set_stats_session_id(id)` or replace `stats_recorder` on switch paths.

  **Minimum v1:** bind `session_id` from the store at construction; on `SessionController` load/switch methods that change `self.store`, update recorder session id:

  ```python
  # on LedgerStatsRecorder
  def set_session_id(self, session_id: str) -> None:
      self._session_id = session_id
  ```

  Call from `SessionController` whenever `self.store` is replaced (find `self.store =` assignments in ctrl.py and hook there).

- **bootstrap:** after reading env, if `os.environ.get("MARIM_STATS", "1").strip() in {"0", "false", "no"}`: pass `stats=False` into `with_sessions`.

- **`.env.example`:** add:
  ```text
  # Stats ledger (per-turn usage JSONL under XDG …/marim-harness/stats/). 0=off.
  # MARIM_STATS=1
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_stats_wiring.py
from __future__ import annotations

from pathlib import Path

from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from marim_harness import HarnessBuilder
from marim_harness.stats.ledger import default_stats_base


def test_with_sessions_writes_stats(tmp_path: Path):
    sessions = tmp_path / "sessions"
    h = (HarnessBuilder(workspace=tmp_path / "ws", model=TestModel())
         .with_sessions(dir=sessions).build())
    assert h.session.stats_recorder is not None
    h.session.add_usage(RunUsage(input_tokens=5, output_tokens=1))
    stats_base = default_stats_base(sessions)
    # dual files under stats_base
    found = sorted(stats_base.rglob("turns.jsonl"))
    assert len(found) == 2  # workspace + global
    bodies = [p.read_text().strip() for p in found]
    assert all(bodies)
    assert bodies[0] == bodies[1]
    assert '"input_tokens":5' in bodies[0] or '"input_tokens": 5' in bodies[0]


def test_with_sessions_stats_false_writes_nothing(tmp_path: Path):
    sessions = tmp_path / "sessions"
    h = (HarnessBuilder(workspace=tmp_path / "ws", model=TestModel())
         .with_sessions(dir=sessions, stats=False).build())
    assert h.session.stats_recorder is None
    h.session.add_usage(RunUsage(input_tokens=5, output_tokens=1))
    stats_base = default_stats_base(sessions)
    assert list(stats_base.rglob("turns.jsonl")) == []


def test_bare_builder_no_stats_recorder(tmp_path: Path):
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).build()
    assert h.session.stats_recorder is None
    h.session.add_usage(RunUsage(input_tokens=1, output_tokens=1))
    # no default XDG pollution asserted here — bare build must not create a
    # ledger object; add_usage without recorder is a pure in-memory +=
```

Pattern mirrors `tests/test_builder.py` (`HarnessBuilder(...).with_sessions(dir=sessions).build()`).

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest --no-cov -n 0 tests/test_stats_wiring.py -v
```

- [ ] **Step 3: Implement wiring**

Touch builder, harness config, build_collaborators, bootstrap, `.env.example`, and session-id updates on store switch.

- [ ] **Step 4: Run tests**

```bash
uv run pytest --no-cov -n 0 tests/test_stats_wiring.py tests/test_stats_add_usage.py tests/test_builder.py -v
uv run ruff check src/marim_harness/runtime src/marim_harness/stats src/marim_harness/session
uv run pyright
```

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/runtime/builder.py \
        src/marim_harness/runtime/harness.py \
        src/marim_harness/runtime/bootstrap.py \
        src/marim_harness/session/ctrl.py \
        src/marim_harness/stats/recorder.py \
        .env.example \
        tests/test_stats_wiring.py
git commit -m "feat(stats): wire ledger through with_sessions and harness"
```

---

### Task 6: SDK docs + final verification

**Files:**
- Modify: `docs/sdk/sessions-and-state.md`
- Optional short note in `CLAUDE.md` under supporting subsystems (one bullet) — only if other subsystems are listed there; keep to 3–5 lines.

- [ ] **Step 1: Update sessions-and-state.md**

In the quick-reference table add:

```markdown
| Stats ledger | off | with sessions (default on; `stats=False` / `MARIM_STATS=0` offs) | sibling `stats/` of sessions base (`with_sessions(dir=...)`) |
```

Add a short `## Stats ledger` section:

- Dual JSONL under `…/marim-harness/stats/{global,<ws-slug>}/turns.jsonl`
- Per-turn deltas via `SessionController.add_usage`
- Query: `marim_harness.stats.load_overview` / `load_models` / pure `overview` / `models`
- No prompts stored; wipe by deleting `stats/`
- Not backfilled from old sessions

- [ ] **Step 2: Full verification**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest --no-cov -n 0 tests/test_stats_query.py tests/test_stats_ledger.py tests/test_stats_add_usage.py tests/test_stats_wiring.py -v
# broader safety:
uv run pytest --no-cov -n 0 tests/test_agent_sessions.py tests/test_builder.py tests/test_usage.py -v
rg -n "session\.usage\s*\+=" src/marim_harness || echo "OK: no bare session.usage +="
```

Expected: all green; only `self.usage +=` lives inside `add_usage`.

- [ ] **Step 3: Commit**

```bash
git add docs/sdk/sessions-and-state.md CLAUDE.md
git commit -m "docs: document stats ledger in sessions-and-state"
```

---

## Spec coverage checklist

| Spec requirement | Task |
| --- | --- |
| Dual JSONL workspace + global | 2 |
| Event schema v1 fields | 2–3 |
| Pure overview/models + ranges | 1 |
| Heatmap 52-week window ignores 7d/30d | 1 |
| current_streak full-set | 1 |
| `add_usage` single seam | 4 |
| Best-effort never fails turn | 2–4 |
| `with_sessions` default on / `stats=False` | 5 |
| `MARIM_STATS=0` | 5 |
| Bare builder no I/O | 5 |
| `load_overview` / `load_models` | 3 |
| SDK docs table row | 6 |
| No TUI / no backfill | — out of scope |
| Aux models excluded (not in session.usage) | automatic |
| UTC days | 1, 3 |

## Placeholder / consistency self-review

- No TBD steps; test code included for each task.
- Names locked: `TurnEvent`, `Overview`, `ModelsReport`, `StatsLedger`, `LedgerStatsRecorder`, `add_usage`, `duration_snapshot`, `default_stats_base`, `workspace_slug`, `load_overview`, `load_models`.
- Task 5 explicitly defers to `tests/test_builder.py` for builder construction patterns rather than inventing a broken harness fixture.
- `StatsLedger.workspace_slug` attribute required by recorder (Task 2–3).
