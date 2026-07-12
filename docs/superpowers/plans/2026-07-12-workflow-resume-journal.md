# Workflow Resumability Journal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Journal every successful `agent()` result of a workflow run so a failed/timed-out/interrupted run can be resumed with `run_workflow(..., resume="<old id>")`, reusing cached results instead of re-spending sub-agents.

**Architecture:** A new `workflows/journal.py` owns content-addressed keying (`entry_key`), the per-run `Journal`, the `ReplayCache`, the on-disk `JournalStore` (sibling of `session/transcripts.py`'s `TranscriptStore`), and the session-bound `WorkflowJournals` (sibling of `subagents/persistence.py`'s `SpawnTranscripts`). The engine appends to the journal after each successful spawn and consults the cache before spawning; the `resume` id is validated in the engine *before* the run is announced. The tool grows a `resume` parameter threaded through the `services.run_workflow` seam. A fold-in closes the recorded Minor: spawn sidecar meta records `output_schema` and `resume_spawn` passes it through on rebuild.

**Tech Stack:** Python 3.10+, pydantic-monty (existing `[workflows]` extra — journal.py itself must NOT import it), pytest + anyio, FunctionModel fakes. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-12-workflow-resumability-journal-design.md` — read it before starting; it is the authority on semantics.

## Global Constraints

- Use `uv` for everything (`uv run pytest`, `uv run ruff check src tests`, `uv run pyright`). Never bare `python`/`pip`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity cap 10 — extract helpers rather than `# noqa: C901`.
- `requires-python >=3.10`: no 3.11+-only syntax.
- Tests use fakes/FunctionModel/TestModel only — NEVER a paid model.
- Coverage gate is 90% (on by default via pyproject).
- Journal persistence is best-effort: log a warning and degrade, never raise into a run (same posture as `TranscriptStore`).
- `journal.py` must not import `pydantic_monty` (it must be importable when the extra is absent).
- The no-journal `resume` error must be returned BEFORE `_announce_start` (no card claimed), exact text: `No journal found for run '<id>' — it may predate journaling or belong to another session. Re-run without resume.`
- Schema'd calls journal the RAW report string; replay re-runs `validate_report`.
- Only successful reports are journaled — never failures, never `log()` lines.
- Commit messages end with exactly these two lines:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN`
- `git add` only the files your task names — never `git add -A` or `git add .`.

---

### Task 1: `workflows/journal.py` — keying, Journal, ReplayCache, stores

**Files:**
- Create: `src/marim_harness/workflows/journal.py`
- Test: `tests/test_workflow_journal.py`

**Interfaces:**
- Consumes: `marim_harness.atomic_io.atomic_write_text(path, text)`, `marim_harness.session.transcripts._safe(stream_id) -> str` (the filename sanitizer — import it, do not duplicate).
- Produces (Tasks 2–3 rely on these exact signatures):
  - `entry_key(task: str, type: str, model, schema, isolation, max_output_chars) -> str`
  - `Journal(tool_call_id: str, script_title: str)` with `.append(key, type, task, report)`, `.entries: list[dict]`, `.to_payload() -> dict`, `Journal.from_payload(payload) -> Journal | None`
  - `ReplayCache(journal)` with `.take(key) -> str | None`, `.remaining: int`
  - `JournalStore(session_path, session_id)` with `.save(journal) -> None`, `.load(run_id) -> Journal | None`
  - `WorkflowJournals(session)` with `.save(journal) -> None`, `.load(run_id) -> Journal | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workflow_journal.py
"""Keying, journal round-trip, replay consumption, and store degradation."""

from pathlib import Path

from marim_harness.workflows.journal import (
    Journal,
    JournalStore,
    ReplayCache,
    entry_key,
)

_ARGS = dict(task="do x", type="explore", model=None, schema=None,
             isolation=None, max_output_chars=None)


def test_entry_key_is_stable_and_content_sensitive():
    base = entry_key(**_ARGS)
    assert base == entry_key(**_ARGS)  # deterministic
    for field, value in [("task", "do y"), ("type", "general"),
                         ("model", "m1"), ("isolation", "worktree"),
                         ("max_output_chars", 100)]:
        assert entry_key(**{**_ARGS, field: value}) != base


def test_entry_key_ignores_schema_dict_ordering():
    s1 = {"type": "object", "properties": {"a": {"type": "string"}}}
    s2 = {"properties": {"a": {"type": "string"}}, "type": "object"}
    assert (entry_key(**{**_ARGS, "schema": s1})
            == entry_key(**{**_ARGS, "schema": s2}))
    assert entry_key(**{**_ARGS, "schema": s1}) != entry_key(**_ARGS)


def test_journal_round_trips_through_payload():
    j = Journal("tc1", "my script")
    j.append(entry_key(**_ARGS), "explore", "do x" * 100, "the report")
    payload = j.to_payload()
    assert payload["v"] == 1
    assert payload["meta"]["tool_call_id"] == "tc1"
    assert payload["entries"][0]["report"] == "the report"
    # task_preview is display-only and bounded
    assert len(payload["entries"][0]["task_preview"]) <= 120
    back = Journal.from_payload(payload)
    assert back is not None and back.entries == j.entries


def test_from_payload_rejects_garbage():
    assert Journal.from_payload(None) is None
    assert Journal.from_payload([]) is None
    assert Journal.from_payload({"v": 2, "meta": {}, "entries": []}) is None
    assert Journal.from_payload({"v": 1, "meta": {}, "entries": "no"}) is None


def test_replay_cache_consumes_duplicates_in_order():
    j = Journal("tc1", "t")
    k = entry_key(**_ARGS)
    j.append(k, "explore", "do x", "first")
    j.append(k, "explore", "do x", "second")
    cache = ReplayCache(j)
    assert cache.remaining == 2
    assert cache.take(k) == "first"
    assert cache.take(k) == "second"
    assert cache.take(k) is None
    assert cache.take("no-such-key") is None
    assert cache.remaining == 0


def test_store_round_trip_and_missing(tmp_path: Path):
    store = JournalStore(tmp_path / "sessions" / "s.json", "sess-1")
    j = Journal("tc-x/1", "t")  # id with an unsafe char exercises _safe
    j.append(entry_key(**_ARGS), "explore", "do x", "r")
    store.save(j)
    back = store.load("tc-x/1")
    assert back is not None and back.entries == j.entries
    assert store.load("never-ran") is None


def test_store_load_returns_none_on_corrupt_file(tmp_path: Path):
    store = JournalStore(tmp_path / "sessions" / "s.json", "sess-1")
    j = Journal("tc1", "t")
    store.save(j)
    # Corrupt the file on disk, then load must degrade to None.
    files = list((tmp_path / "sessions" / "sess-1.workflows").iterdir())
    files[0].write_text("{not json")
    assert store.load("tc1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_workflow_journal.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.workflows.journal'`

- [ ] **Step 3: Write the implementation**

```python
# src/marim_harness/workflows/journal.py
"""Per-run journals of completed agent() results, for workflow resume.

A workflow run that times out, raises, or is interrupted loses nothing it
already paid for: every successful agent() report is appended here and
persisted after each append, so a later run_workflow(..., resume=<old id>)
can replay them instead of re-spending the sub-agents. Matching is
content-addressed (see entry_key) — robust to asyncio.gather reordering and
to script edits: an unchanged call hits the cache wherever it moved.

This module must stay importable without the [workflows] extra: the engine
imports IT, never the reverse, and it must not import pydantic_monty.
Persistence mirrors session/transcripts.py: one file per run under
``<session_path.parent>/<session_id>.workflows/``, atomic writes,
best-effort everywhere (log and degrade, never raise into a run)."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..atomic_io import atomic_write_text
from ..session.transcripts import _safe

if TYPE_CHECKING:
    from ..session.ctrl import SessionController

logger = logging.getLogger(__name__)

_TASK_PREVIEW_CHARS = 120


def entry_key(task: str, type: str, model, schema, isolation,
              max_output_chars) -> str:
    """Content hash identifying one agent() call: two calls share a key iff
    every argument that shapes the spawn is equal. sort_keys canonicalizes
    dict ordering inside ``schema`` so equivalent schemas can't split keys.
    Pure; unit-tested directly."""
    payload = json.dumps(
        {"task": task, "type": type, "model": model, "schema": schema,
         "isolation": isolation, "max_output_chars": max_output_chars},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Journal:
    """One run's ordered record of successful agent() results. ``report`` is
    always the raw string the spawn returned — schema'd calls journal the raw
    report and replay re-validates, so cached and live semantics match."""

    def __init__(self, tool_call_id: str, script_title: str, *,
                 entries: list[dict] | None = None,
                 created: str | None = None) -> None:
        self.tool_call_id = tool_call_id
        self.script_title = script_title
        self.created = created or datetime.now(timezone.utc).isoformat()
        self.entries: list[dict] = entries if entries is not None else []

    def append(self, key: str, type: str, task: str, report: str) -> None:
        self.entries.append({
            "key": key, "type": type,
            "task_preview": task[:_TASK_PREVIEW_CHARS],
            "report": report,
        })

    def to_payload(self) -> dict:
        return {
            "v": 1,
            "meta": {
                "tool_call_id": self.tool_call_id,
                "script_title": self.script_title,
                "created": self.created,
                "updated": datetime.now(timezone.utc).isoformat(),
            },
            "entries": self.entries,
        }

    @classmethod
    def from_payload(cls, payload: object) -> Journal | None:
        """A Journal from a loaded file payload, or None when the shape is not
        a v1 envelope (corrupt file, future format)."""
        if not isinstance(payload, dict) or payload.get("v") != 1:
            return None
        meta, entries = payload.get("meta"), payload.get("entries")
        if not isinstance(meta, dict) or not isinstance(entries, list):
            return None
        return cls(
            str(meta.get("tool_call_id") or ""),
            str(meta.get("script_title") or ""),
            entries=[e for e in entries if isinstance(e, dict)],
            created=meta.get("created"),
        )


class ReplayCache:
    """Journaled reports indexed for replay: ``take`` pops the oldest
    unconsumed entry for a key. Duplicate identical calls consume entries in
    journal order — deterministic because appends happen on the
    single-threaded event loop."""

    def __init__(self, journal: Journal) -> None:
        self._by_key: dict[str, deque[str]] = {}
        for e in journal.entries:
            key, report = e.get("key"), e.get("report")
            if isinstance(key, str) and isinstance(report, str):
                self._by_key.setdefault(key, deque()).append(report)

    @property
    def remaining(self) -> int:
        return sum(len(d) for d in self._by_key.values())

    def take(self, key: str) -> str | None:
        d = self._by_key.get(key)
        return d.popleft() if d else None


class JournalStore:
    """Reads/writes one journal per run under
    ``<session_path.parent>/<session_id>.workflows/<safe id>.json`` — the
    exact sibling of TranscriptStore's layout and posture: atomic writes,
    best-effort methods that log and degrade instead of raising."""

    def __init__(self, session_path, session_id: str) -> None:
        self._dir = Path(session_path).parent / f"{session_id}.workflows"

    def _file(self, run_id: str) -> Path:
        return self._dir / f"{_safe(run_id)}.json"

    def save(self, journal: Journal) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._file(journal.tool_call_id),
                              json.dumps(journal.to_payload()))
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("Failed to write workflow journal %s: %s",
                           journal.tool_call_id, exc)

    def load(self, run_id: str) -> Journal | None:
        try:
            text = self._file(run_id).read_text(encoding="utf-8")
            return Journal.from_payload(json.loads(text))
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001 - reads are best-effort
            logger.warning("Failed to read workflow journal %s: %s",
                           run_id, exc)
            return None


class WorkflowJournals:
    """Session-bound journal persistence for one engine. Holds the session
    controller (not a fixed store) so reads and writes always target the
    session active right now — the same live-resolution rule
    SpawnTranscripts follows for spawn sidecars."""

    def __init__(self, session: SessionController) -> None:
        self._session = session

    def _store(self) -> JournalStore | None:
        store = self._session.store
        if store is None:
            return None
        return JournalStore(store.path, store.session_id)

    def save(self, journal: Journal) -> None:
        store = self._store()
        if store is not None:
            store.save(journal)

    def load(self, run_id: str) -> Journal | None:
        store = self._store()
        return store.load(run_id) if store is not None else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_workflow_journal.py -v`
Expected: 7 passed

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src/marim_harness/workflows/journal.py tests/test_workflow_journal.py && uv run pyright`
Expected: clean.

```bash
git add src/marim_harness/workflows/journal.py tests/test_workflow_journal.py
git commit -m "feat(workflows): journal module — entry_key, Journal, ReplayCache, stores

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

### Task 2: Engine integration — journal appends, replay lookup, resume guard, outcome hints

**Files:**
- Modify: `src/marim_harness/workflows/engine.py`
- Test: `tests/test_workflow_engine.py` (append new tests; do not modify existing ones)

**Interfaces:**
- Consumes (from Task 1): `entry_key`, `Journal`, `ReplayCache`; a `journals` object exposing `.save(journal)` and `.load(run_id) -> Journal | None` (duck-typed — tests pass an in-memory fake, production passes `WorkflowJournals`).
- Produces (Task 3 relies on): `WorkflowEngine.__init__(deps, spawn, *, timeout_secs=DEFAULT_TIMEOUT_SECS, journals=None)` and `WorkflowEngine.run(script, args, tool_call_id, timeout_secs=None, resume=None) -> str`.

**Behavior to implement (spec §Data flow, §Outcome messages, §UI treatment):**
1. `run()` gains `resume: str | None = None`. After parse/type-check but BEFORE `_announce_start`: if `resume` is set, load the prior journal; on failure return the exact no-journal error (Global Constraints) — no card claimed. On success build a `ReplayCache` and log `journal: loaded {n} cached result(s) from {resume}` right after `_announce_start`.
2. Every run (resumed or not) records a `Journal` when `journals` is present; each successful `agent()` result appends an entry and saves the whole journal (atomic rewrite).
3. In `_agent_call`, before spawning: compute the key; on a cache hit return the cached value (re-validated when `schema` is given; a validation failure falls through to a live spawn). Cached hits create no child task and announce no spawn card.
4. Timeout and script-raise outcomes append, when at least one entry is journaled: ` Resume with resume="<tool_call_id>" to reuse the {k} completed sub-agent result(s).`
5. Terminal announce on resumed runs is preceded by one log line: `journal: reused {k} cached result(s), {m} ran live`.
6. Keep `run()` and `_agent_call` under the C901 ceiling by extracting the named helpers below.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_workflow_engine.py`; reuse its `_engine` helper and `_make_deps`)

```python
# --- resumability journal ---------------------------------------------------

from marim_harness.workflows.journal import Journal


class _MemJournals:
    """In-memory stand-in for WorkflowJournals: same save/load surface."""

    def __init__(self):
        self.saved: dict[str, dict] = {}

    def save(self, journal):
        self.saved[journal.tool_call_id] = journal.to_payload()

    def load(self, run_id):
        payload = self.saved.get(run_id)
        return Journal.from_payload(payload) if payload is not None else None


def _journaled_engine(tmp_path, spawn, **kw):
    # WorkflowEngine and _make_deps are already imported at the top of this
    # test file — do not re-import.
    deps = _make_deps(tmp_path)
    journals = _MemJournals()
    return WorkflowEngine(deps, spawn, journals=journals, **kw), deps, journals


_TWO_CALLS = 'a = await agent("task a")\nb = await agent("task b")\n[a, b]'


@pytest.mark.anyio
async def test_successful_results_are_journaled(tmp_path):
    eng, _, journals = _journaled_engine(tmp_path, _echo_spawn)
    await eng.run(_TWO_CALLS, None, "tc1")
    entries = journals.saved["tc1"]["entries"]
    assert len(entries) == 2
    assert entries[0]["task_preview"] == "task a"
    assert "[general@0] task a" in entries[0]["report"]


@pytest.mark.anyio
async def test_full_hit_resume_spawns_no_children(tmp_path):
    eng, _, journals = _journaled_engine(tmp_path, _echo_spawn)
    first = await eng.run(_TWO_CALLS, None, "tc1")
    calls = 0

    async def exploding_spawn(*a, **kw):
        nonlocal calls
        calls += 1
        raise AssertionError("cached run must not spawn")

    eng2 = WorkflowEngine(_make_deps(tmp_path), exploding_spawn,
                          journals=journals)
    second = await eng2.run(_TWO_CALLS, None, "tc2", resume="tc1")
    assert calls == 0
    assert second == first
    # The resumed run re-journals under its own id (chained resumes).
    assert len(journals.saved["tc2"]["entries"]) == 2


@pytest.mark.anyio
async def test_partial_hit_runs_only_changed_calls_live(tmp_path):
    eng, _, journals = _journaled_engine(tmp_path, _echo_spawn)
    await eng.run(_TWO_CALLS, None, "tc1")
    live: list[str] = []

    async def spawn(type, task, *rest, **kw):
        live.append(task)
        return "live " + task

    eng2 = WorkflowEngine(_make_deps(tmp_path), spawn, journals=journals)
    edited = 'a = await agent("task a")\nb = await agent("task EDITED")\n[a, b]'
    out = await eng2.run(edited, None, "tc2", resume="tc1")
    assert live == ["task EDITED"]
    assert "[general@0] task a" in out and "live task EDITED" in out


@pytest.mark.anyio
async def test_duplicate_identical_calls_consume_entries_in_order(tmp_path):
    reports = iter(["r1", "r2"])

    async def spawn(*a, **kw):
        return next(reports)

    eng, _, journals = _journaled_engine(tmp_path, spawn)
    dup = 'a = await agent("same")\nb = await agent("same")\n[a, b]'
    await eng.run(dup, None, "tc1")
    eng2 = WorkflowEngine(_make_deps(tmp_path),
                          _echo_spawn, journals=journals)
    out = await eng2.run(dup, None, "tc2", resume="tc1")
    assert '"r1"' in out and '"r2"' in out


@pytest.mark.anyio
async def test_schema_cached_report_is_revalidated(tmp_path):
    async def spawn(*a, **kw):
        return '{"n": 1}'

    eng, _, journals = _journaled_engine(tmp_path, spawn)
    script = (
        'r = await agent("q", schema={"type": "object", '
        '"properties": {"n": {"type": "integer"}}, "required": ["n"]})\n'
        "r['n']"
    )
    await eng.run(script, None, "tc1")

    async def exploding_spawn(*a, **kw):
        raise AssertionError("must replay from journal")

    eng2 = WorkflowEngine(_make_deps(tmp_path), exploding_spawn,
                          journals=journals)
    out = await eng2.run(script, None, "tc2", resume="tc1")
    assert "1" in out


@pytest.mark.anyio
async def test_unknown_resume_id_errors_before_announcing(tmp_path):
    events: list = []
    eng, deps, _ = _journaled_engine(tmp_path, _echo_spawn)
    deps.ui.on_workflow_start = lambda *a: events.append(("start", *a))
    out = await eng.run("1 + 1", None, "tc2", resume="never-ran")
    assert "No journal found for run 'never-ran'" in out
    assert events == []


@pytest.mark.anyio
async def test_resume_without_journals_wiring_errors_cleanly(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)  # journals=None
    out = await eng.run("1 + 1", None, "tc2", resume="tc1")
    assert "No journal found for run 'tc1'" in out


@pytest.mark.anyio
async def test_timeout_outcome_advertises_resume_id(tmp_path):
    async def slow_after_first(type, task, *rest, **kw):
        if task == "fast":
            return "done-fast"
        await asyncio.sleep(5)
        return "never"

    eng, _, journals = _journaled_engine(tmp_path, slow_after_first)
    script = ('a = await agent("fast")\n'
              'b = await agent("slow")\n[a, b]')
    out = await eng.run(script, None, "tc1", timeout_secs=0.5)
    assert "timed out" in out
    assert 'resume="tc1"' in out and "1 completed sub-agent result" in out


@pytest.mark.anyio
async def test_script_raise_outcome_advertises_resume_id(tmp_path):
    eng, _, journals = _journaled_engine(tmp_path, _echo_spawn)
    script = 'a = await agent("ok")\nraise ValueError("boom")'
    out = await eng.run(script, None, "tc1")
    assert "Workflow script raised" in out
    assert 'resume="tc1"' in out


@pytest.mark.anyio
async def test_no_hint_when_nothing_was_journaled(tmp_path):
    eng, _, journals = _journaled_engine(tmp_path, _echo_spawn)
    out = await eng.run('raise ValueError("boom")', None, "tc1")
    assert "resume=" not in out


@pytest.mark.anyio
async def test_replay_logs_loaded_and_summary_lines(tmp_path):
    logs: list[str] = []
    eng, _, journals = _journaled_engine(tmp_path, _echo_spawn)
    await eng.run(_TWO_CALLS, None, "tc1")
    deps2 = _make_deps(tmp_path)
    deps2.ui.on_workflow_log = lambda tcid, message: logs.append(message)
    eng2 = WorkflowEngine(deps2, _echo_spawn, journals=journals)
    await eng2.run(_TWO_CALLS, None, "tc2", resume="tc1")
    assert any("loaded 2 cached result(s) from tc1" in m for m in logs)
    assert any("reused 2 cached result(s), 0 ran live" in m for m in logs)


@pytest.mark.anyio
async def test_abort_mid_run_keeps_journal_for_resume(tmp_path):
    started = asyncio.Event()

    async def spawn(type, task, *rest, **kw):
        if task == "fast":
            return "done-fast"
        started.set()
        await asyncio.sleep(30)
        return "never"

    eng, _, journals = _journaled_engine(tmp_path, spawn)
    script = ('a = await agent("fast")\n'
              'b = await agent("hang")\n[a, b]')
    run = asyncio.ensure_future(eng.run(script, None, "tc1"))
    await asyncio.wait_for(started.wait(), 5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    entries = journals.saved["tc1"]["entries"]
    assert len(entries) == 1 and entries[0]["task_preview"] == "fast"
```

Note: `test_timeout_outcome_advertises_resume_id` orders its calls
sequentially (not gather) so the fast result is journaled before the hang.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py -k "journal or resume or hint or replay" -v`
Expected: FAIL — `run() got an unexpected keyword argument 'resume'` / `__init__() got an unexpected keyword argument 'journals'`.

- [ ] **Step 3: Implement in `engine.py`**

3a. Import from the journal module (top of file, with the other relative imports):

```python
from .journal import Journal, ReplayCache, entry_key
```

3b. Extend `_RunState`:

```python
@dataclass
class _RunState:
    """Per-run mutable state shared by the host functions."""

    tool_call_id: str
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    children: set[asyncio.Task] = field(default_factory=set)
    seq: int = 0
    # Resumability journal (spec 2026-07-12-workflow-resumability-journal).
    # journal records this run's successful results; replay serves a prior
    # run's results on resume. reused/live feed the end-of-run summary line.
    journal: Journal | None = None
    replay: ReplayCache | None = None
    reused: int = 0
    live: int = 0
```

3c. Constructor gains the journals seam:

```python
    def __init__(self, deps: Deps, spawn, *, timeout_secs: float = DEFAULT_TIMEOUT_SECS,
                 journals=None):
        self.deps = deps
        self._spawn = spawn
        # ... (existing self._timeout comment/assignment unchanged) ...
        # Session-bound journal persistence (WorkflowJournals), or None —
        # headless/tests without sessions journal nothing and any resume
        # request answers with the no-journal error. Duck-typed on purpose:
        # tests pass an in-memory fake with the same save/load surface.
        self._journals = journals
```

3d. New helpers (place near `_effective_timeout`; keep them small and pure-ish):

```python
    def _start_replay(self, resume: str) -> ReplayCache | str:
        """The prior run's results indexed for replay, or the correctable
        error returned when no journal can be loaded. Runs BEFORE the run is
        announced, so a bad resume id never claims a card."""
        prior = self._journals.load(resume) if self._journals is not None else None
        if prior is None:
            return (f"No journal found for run '{resume}' — it may predate "
                    "journaling or belong to another session. Re-run without "
                    "resume.")
        return ReplayCache(prior)

    def _journal_append(self, state: _RunState, key: str, type: str,
                        task: str, report: str, *, reused: bool = False) -> None:
        """Record one successful agent() result and persist the journal.
        Every append rewrites the file atomically — a crash loses at most the
        in-flight spawns, never corrupts what was already journaled."""
        if reused:
            state.reused += 1
        else:
            state.live += 1
        if state.journal is None:
            return
        state.journal.append(key, type, task, report)
        if self._journals is not None:
            self._journals.save(state.journal)

    def _take_cached(self, state: _RunState, key: str, schema) -> tuple[str, object] | None:
        """(raw report, value to return) from the replay cache, or None on a
        miss. Schema'd calls re-validate the cached raw report so cached and
        live semantics match; a report that no longer validates falls through
        to a live spawn (the consumed entry can't match another call — the
        schema is part of the key)."""
        if state.replay is None:
            return None
        cached = state.replay.take(key)
        if cached is None:
            return None
        if schema is None:
            return cached, cached
        data, err = validate_report(cached, schema)
        if err is not None:
            return None
        return cached, data

    def _resume_hint(self, state: _RunState) -> str:
        """The resume instruction appended to failure outcomes — only when
        something was actually journaled, so a run with nothing to reuse
        doesn't advertise a pointless resume."""
        if state.journal is None or not state.journal.entries:
            return ""
        n = len(state.journal.entries)
        return (f' Resume with resume="{state.tool_call_id}" to reuse the '
                f"{n} completed sub-agent result(s).")

    def _finish(self, state: _RunState, outcome: str, *, failed: bool) -> None:
        """Terminal announce, preceded on resumed runs by the one-line
        reuse summary (spec §UI treatment)."""
        if state.replay is not None:
            self._log(state.tool_call_id,
                      f"journal: reused {state.reused} cached result(s), "
                      f"{state.live} ran live")
        self._announce_done(state.tool_call_id, outcome, failed=failed)
```

3e. In `run()` — signature and the resume guard (between the type-check block and `_announce_start`); then create the journal on the state and route every `_announce_done(tool_call_id, ...)` call in `run()` through `self._finish(state, ...)`:

```python
    async def run(self, script: str, args: object, tool_call_id: str,
                  timeout_secs: float | None = None,
                  resume: str | None = None) -> str:
        # ... existing parse + type_check blocks unchanged ...
        replay: ReplayCache | None = None
        if resume:
            loaded = self._start_replay(resume)
            if isinstance(loaded, str):
                return loaded
            replay = loaded
        self._announce_start(tool_call_id, _script_title(script))
        state = _RunState(
            tool_call_id=tool_call_id, replay=replay,
            journal=(Journal(tool_call_id, _script_title(script))
                     if self._journals is not None else None),
        )
        if replay is not None:
            self._log(tool_call_id,
                      f"journal: loaded {replay.remaining} cached result(s) "
                      f"from {resume}")
        # ... effective/vm_limits/prints/vm blocks unchanged ...
```

Outcome changes inside `run()` (each existing `_announce_done` becomes `_finish`):
- Cancel path: `self._finish(state, "workflow aborted", failed=True)` then `raise`.
- Timeout path: `outcome = (f"Workflow timed out after {effective:.0f}s; in-flight sub-agents were cancelled." + self._resume_hint(state))` then `self._finish(state, outcome, failed=True)`.
- Raise path: `outcome = f"Workflow script raised: {exc}\n\n{exc.display()}" + self._resume_hint(state)` then `self._finish(state, outcome, failed=True)`.
- Success path: `self._finish(state, shaped, failed=False)`.

If `run()` trips C901 after this, extract the parse+type-check prelude into a `_preflight(script) -> Monty | str` helper (returns the error string to hand back verbatim) — do NOT `# noqa`.

3f. In `_agent_call` — cache lookup before spawning, journal appends on every success return. Full replacement body:

```python
    async def _agent_call(self, state: _RunState, task: str, *, type: str,
                          model, schema, max_output_chars, isolation):
        if schema is not None:
            check_valid_schema(schema)
        key = entry_key(task=task, type=type, model=model, schema=schema,
                        isolation=isolation, max_output_chars=max_output_chars)
        hit = self._take_cached(state, key, schema)
        if hit is not None:
            raw, value = hit
            self._journal_append(state, key, type, task, raw, reused=True)
            return value
        # (existing comment about enforcement riding the spawn seam stays)
        report = await self._spawn_child(
            state, type, task, max_output_chars, model, isolation, schema,
        )
        if schema is None:
            self._journal_append(state, key, type, task, report)
            return report
        data, err = validate_report(report, schema)
        for _ in range(_SCHEMA_RETRIES):
            if err is None:
                break
            retry_task = (
                task
                + f"\n\nA previous attempt failed validation: {err}. "
                  "Respond again with ONLY the corrected JSON."
            )
            report = await self._spawn_child(
                state, type, retry_task, max_output_chars, model, isolation, schema,
            )
            data, err = validate_report(report, schema)
        if err is None:
            self._journal_append(state, key, type, task, report)
            return data
        raise WorkflowResultError(
            f"agent() output failed schema validation after a retry: {err}"
        )
```

(The retry-loop restructure from `if err is None: return data` at loop top to `break` is behavior-preserving: same number of retries, same final check.)

- [ ] **Step 4: Run the workflow test files**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py tests/test_workflow_journal.py tests/test_workflow_acceptance.py -v`
Expected: all pass (existing tests must stay green — the new parameters default off).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean (C901 included).

```bash
git add src/marim_harness/workflows/engine.py tests/test_workflow_engine.py
git commit -m "feat(workflows): journal successful agent() results; replay them on resume

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

### Task 3: Tool `resume` parameter, seam alias, harness wiring, docstring copy

**Files:**
- Modify: `src/marim_harness/tools/workflow_tools.py`
- Modify: `src/marim_harness/runtime/deps.py` (the `WorkflowRunner` alias, ~line 71)
- Modify: `src/marim_harness/runtime/harness.py` (`_build_workflow_engine`, ~line 183, and its call site, ~line 387)
- Test: `tests/test_workflow_tool.py`, `tests/test_workflow_wiring.py` (append)

**Interfaces:**
- Consumes (Task 2): `WorkflowEngine.run(script, args, tool_call_id, timeout_secs=None, resume=None)`; (Task 1): `WorkflowJournals(session)`.
- Produces: `run_workflow(ctx, script, args=None, timeout_secs=None, resume=None)`; `WorkflowRunner = Callable[[str, object, str, float | None, str | None], Awaitable[str]]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_tool.py` (the file's `_ctx(deps, tool_call_id="tc1")` helper wraps deps in a SimpleNamespace; fake runners install via `deps.services.run_workflow`):

```python
@pytest.mark.anyio
async def test_resume_threads_through_the_seam(tmp_path):
    deps = _make_deps(tmp_path)
    seen: dict = {}

    async def fake_runner(script, args, tool_call_id, timeout_secs, resume):
        seen.update(resume=resume, timeout_secs=timeout_secs)
        return "ok"

    deps.services.run_workflow = fake_runner
    out = await run_workflow(_ctx(deps), "1 + 1", resume="tc-old")
    assert out == "ok"
    assert seen == {"resume": "tc-old", "timeout_secs": None}


def test_docstring_documents_resume():
    """The resume paragraph is model-facing product copy: it must name the
    parameter, tie it to tool_call_id, and state the unchanged-calls-are-
    cached matching rule."""
    doc = run_workflow.__doc__ or ""
    assert "resume" in doc
    assert "tool_call_id" in doc
    assert "unchanged" in doc
```

The seam change widens the runner signature to five arguments, so the three
existing tests with four-arg fakes break. Update them in the same commit:
`test_delegates_script_args_and_tool_call_id` (add `resume` to the fake's
signature, `seen`, and the expected dict — its value is `None`),
`test_timeout_secs_is_forwarded`, and `test_invalid_timeout_is_rejected_without_running`
(its `fake_runner(*a)` already absorbs the extra arg — no change needed).

Append to `tests/test_workflow_wiring.py` (`services.run_workflow` holds the
engine's bound `run` method, so the engine is its `__self__` — the same trick
`test_harness_threads_workflow_timeout_to_the_engine` uses):

```python
def test_engine_is_wired_with_session_journals(tmp_path):
    from marim_harness.workflows.journal import WorkflowJournals

    h: Harness = _make_harness(TestModel(), _make_deps(tmp_path))
    runner = h.deps.services.run_workflow
    assert runner is not None
    assert isinstance(runner.__self__._journals, WorkflowJournals)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_workflow_tool.py tests/test_workflow_wiring.py -v`
Expected: new tests FAIL (unexpected kwarg `resume` / missing `_journals` wiring). Pre-existing tests with four-arg fake runners may also fail after the seam change lands — update those fakes to accept `resume` in the same commit.

- [ ] **Step 3: Implement**

3a. `deps.py` — replace the alias and its comment:

```python
# (script, args, tool_call_id, requested timeout_secs | None, resume run-id |
# None) -> tool result. resume replays a prior run's journaled agent()
# results (see workflows/journal.py). None when workflows are disabled
# (MARIM_WORKFLOWS=0) or pydantic-monty is not installed — the run_workflow
# tool returns an install hint in that case. Wired by the Harness (see
# _build_workflow_engine).
WorkflowRunner = Callable[[str, object, str, float | None, str | None], Awaitable[str]]
```

3b. `workflow_tools.py` — signature and forwarding:

```python
async def run_workflow(
    ctx: RunContext[Deps], script: str, args: JsonValue = None,
    timeout_secs: float | None = None, resume: str | None = None,
) -> str:
```

```python
    return await runner(script, args, ctx.tool_call_id or "", timeout_secs, resume)
```

3c. `workflow_tools.py` — docstring: insert this paragraph directly after the existing `timeout_secs` paragraph ("...clamped to a harness-configured ceiling."):

```
    To RESUME a failed run without re-spending its completed sub-agents,
    pass `resume` = the earlier run_workflow call's tool_call_id. Failure
    results advertise it; for a run that was interrupted before returning,
    use that call's own tool_call_id from your history. Journaled agent()
    results are reused for calls whose task and options are unchanged;
    changed or new calls run live — so you can edit the script (fix a bug,
    raise timeout_secs) and still keep every result already paid for.
```

3d. `harness.py` — `_build_workflow_engine` gains the session controller and wires journals (call site becomes `_build_workflow_engine(cfg, deps, subagents, session)`):

```python
def _build_workflow_engine(cfg: HarnessConfig, deps: Deps,
                           subagents: SubagentRunner, session):
    """The workflow engine, or None when disabled or pydantic-monty is not
    installed. The import is guarded HERE (not in the tool) so availability
    is decided once at build time and the tool only checks the seam."""
    if not cfg.workflows_enabled:
        return None
    try:
        from ..workflows.engine import WorkflowEngine
    except ImportError as exc:
        if exc.name == "pydantic_monty":
            logger.info(
                "workflows unavailable: pydantic-monty not installed "
                "(uv add 'marim-harness[workflows]')"
            )
        else:
            logger.info("workflows unavailable: %s", exc)
        return None
    from ..workflows.journal import WorkflowJournals
    return WorkflowEngine(deps, subagents.run,
                          timeout_secs=cfg.workflow_timeout_secs,
                          journals=WorkflowJournals(session))
```

- [ ] **Step 4: Run the affected test files**

Run: `uv run pytest --no-cov tests/test_workflow_tool.py tests/test_workflow_wiring.py tests/test_workflow_engine.py -v`
Expected: all pass.

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean.

```bash
git add src/marim_harness/tools/workflow_tools.py src/marim_harness/runtime/deps.py \
        src/marim_harness/runtime/harness.py tests/test_workflow_tool.py \
        tests/test_workflow_wiring.py
git commit -m "feat(workflows): run_workflow resume parameter wired through the seam

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

### Task 4: Fold-in — spawn sidecar meta records `output_schema`; `resume_spawn` passes it through

**Files:**
- Modify: `src/marim_harness/subagents/runner.py` (`_prepare_spawn` meta template, ~line 614; `resume_spawn`'s `_prepare_spawn` call, ~line 885)
- Test: `tests/test_subagent_resume.py` (append)

**Interfaces:**
- Consumes: `_prepare_spawn(..., output_schema: dict | None = None)` (already exists); sidecar meta dict (v2 envelope).
- Produces: sidecar meta gains `"output_schema": output_schema`; a resumed spawn rebuilds with the schema it was started with.

**Context:** This closes a recorded Minor from the deep-research batch: a schema'd spawn interrupted mid-run currently resumes WITHOUT structured output, because the meta template never recorded `output_schema` and `resume_spawn` rebuilds from meta alone. The value recorded is the RESOLVED schema `_prepare_spawn` received (post `resolve_output_schema`), so the resume path must NOT re-resolve it.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_subagent_resume.py`; reuse `_session_store`, `_interrupted_meta`, `_dangling_history`, `_resume_model`, `_make_deps`, `_make_harness`)

Add `from pydantic_ai.models.test import TestModel` to the file's imports.

```python
_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
           "required": ["ok"]}


@pytest.mark.anyio
async def test_spawn_meta_records_output_schema(tmp_path):
    """Every sidecar save of a schema'd spawn carries the resolved schema, so
    resume can rebuild with structured output."""
    store = _session_store(tmp_path)
    # TestModel with custom_output_args rides the native StructuredDict path —
    # the same fake test_subagent_output_schema.py uses.
    harness = _make_harness(TestModel(call_tools=[], custom_output_args={"ok": True}),
                            _make_deps(tmp_path), store=store)
    seen: list[dict | None] = []
    orig = harness.subagents._transcripts.save

    def spy(stream_id, messages, meta=None, **kw):
        seen.append(meta)
        orig(stream_id, messages, meta=meta, **kw)

    harness.subagents._transcripts.save = spy
    await harness.subagents.run("general", "task", "sg-schema",
                                output_schema=_SCHEMA)
    metas = [m for m in seen if m is not None]
    assert metas and all(m.get("output_schema") == _SCHEMA for m in metas)


@pytest.mark.anyio
async def test_resume_rebuilds_with_recorded_output_schema(tmp_path, monkeypatch):
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    meta = _interrupted_meta("sg-schema")
    meta["output_schema"] = _SCHEMA
    ts.write("sg-schema", _dangling_history(), 2000, meta=meta)

    seen_kwargs: dict = {}
    orig = harness.subagents._prepare_spawn

    async def spy(*a, **kw):
        seen_kwargs.update(kw)
        return await orig(*a, **kw)

    monkeypatch.setattr(harness.subagents, "_prepare_spawn", spy)
    job_id, message = await harness.subagents.resume_spawn("sg-schema")
    assert job_id is not None, message
    assert seen_kwargs.get("output_schema") == _SCHEMA
```

Note for the second test: `_resume_model` returns plain text, which cannot
satisfy structured output — so do NOT await the resumed job; the assertion
on `seen_kwargs` is complete once `resume_spawn` returns (`_prepare_spawn`
runs before the job is registered).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_resume.py -k output_schema -v`
Expected: FAIL — meta lacks `output_schema` / `seen_kwargs.get("output_schema")` is None.

- [ ] **Step 3: Implement in `runner.py`**

3a. In `_prepare_spawn`'s meta template (after `"isolation": ...`):

```python
                "isolation": iso.branch if iso else None,
                # The RESOLVED output schema (post resolve_output_schema), so a
                # resumed spawn rebuilds with structured output instead of
                # silently dropping it. Resume passes it straight through —
                # never re-resolve.
                "output_schema": output_schema,
                "status": "running",
```

3b. In `resume_spawn`'s `_prepare_spawn` call, add the kwarg:

```python
            prep = await self._prepare_spawn(
                type_, task, meta.get("mcp"), meta.get("max_output_chars"),
                meta.get("model"), iso, iso.path if iso else None, stream_id,
                debug=logger.isEnabledFor(logging.DEBUG), t0=time.perf_counter(),
                depth=int(meta.get("depth") or 1), resumed=True,
                output_schema=meta.get("output_schema"),
            )
```

- [ ] **Step 4: Run the resume and output-schema test files**

Run: `uv run pytest --no-cov tests/test_subagent_resume.py tests/test_subagent_output_schema.py -v`
Expected: all pass.

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean.

```bash
git add src/marim_harness/subagents/runner.py tests/test_subagent_resume.py
git commit -m "fix(subagents): record output_schema in sidecar meta; resume rebuilds with it

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

### Task 5: Live interrupt-and-resume smoke (controller-run — NO subagent)

**Files:** none (validation only; evidence captured to the controller scratchpad).

**Constraints:** free local model ONLY (`MARIM_PROVIDER=local`, LM Studio `ornith-1.0-9b`) — never a paid model. Run marim in tmux from this branch's checkout.

- [ ] **Step 1:** Launch a marim TUI session in tmux on the local model, ask mode.
- [ ] **Step 2:** Have the model start a two-round workflow (the deep-research reference pattern: round 1 fans out ~3 researchers with `asyncio.gather`, round 2 verifies) with `timeout_secs=1800`. Approve it. Known Monty gotchas for the script: no `__name__` at runtime (end with a top-level `await`), no `schema=` on this small model (plain-text reports + per-call try/except).
- [ ] **Step 3:** After round 1 completes (workflow card logs it), interrupt the turn (Escape) — the run aborts through the host functions.
- [ ] **Step 4:** Steer the model to re-call `run_workflow` with the SAME script and `resume="<the interrupted call's tool_call_id>"`. Verify in the approval panel/plan text that `resume` is set.
- [ ] **Step 5:** Verify: the workflow card logs `journal: loaded N cached result(s)`; round-1 researchers do NOT re-spawn (no new round-1 children in the sub-agents screen); round 2 runs live; the run completes and the final summary line reports `reused N ..., M ran live`; the journal file exists under the session's `<session_id>.workflows/` directory.
- [ ] **Step 6:** Capture panes for PR notes; tear down the tmux session.

---

## Final verification (after all tasks)

Run in CI order: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: clean, coverage ≥ 90%.
