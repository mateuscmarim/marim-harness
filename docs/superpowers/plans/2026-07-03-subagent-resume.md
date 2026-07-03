# Sub-Agent Spawn Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist per-spawn sidecar meta + mid-run checkpoints and settled-job history so every sub-agent spawn repopulates the sub-agents screen on session resume, interrupted spawns are detected, and they can be manually resumed as background jobs.

**Architecture:** The sidecar (`TranscriptStore`) gains a v2 `{meta, messages}` envelope, written after each model response via a `ProcessHistory` capability instead of only at completion. `JobRegistry` exports settled summaries into a new `jobs` key of the session payload. Replay rebuilds cards for *all* spawns, joins final status from jobs history, and a `scan_meta()` pass flags crashed spawns as `interrupted`. `SubagentRunner.resume_spawn` repairs the persisted transcript and continues it as a background job, wired to a `r` keybinding on the sub-agents screen.

**Tech Stack:** Python 3.10+, Pydantic AI (`ModelMessagesTypeAdapter`, `ProcessHistory`, `FunctionModel` for tests), Textual, pytest + anyio.

**Spec:** `docs/superpowers/specs/2026-07-03-subagent-resume-design.md`. Three deliberate deviations, all simplifications (update the spec inline when implementing Task 2):
1. CLI-demuxed *children* keep v1 (messages-only) sidecars — their card state already replays from the parent transcript, and `child_transcripts()` carries no type/task to build meta from.
2. The meta carries `type`/`mcp` instead of the spec's `granted` tool-name list: resume rebuilds the sub-agent by `type` through `build()` (which re-derives tool reach from the definition and current mode — the same gate every spawn uses) and re-grants MCP by the persisted names (unknown names are already noted by `granted_toolsets`). Persisting resolved tool names would just be a stale copy of what `build()` recomputes.
3. `parent_id` is absent from the meta — the runner doesn't know its caller's stream id, so a card synthesized from meta alone renders top-level.

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`.
- Python floor is 3.10 — no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- Run `uv run ruff check src tests` → `uv run pyright` → `uv run pytest` (that order, all green) before claiming a task done.
- Preserve the long "why" comments around resumability invariants; write new ones in the same style.
- Persistence is always best-effort: a sidecar/jobs write failure logs a warning, never raises into a turn or a resume.

## Verified codebase facts the tasks rely on

- `TranscriptStore` (`src/marim_harness/session/transcripts.py`) writes bare
  `ModelMessagesTypeAdapter` lists to `<session>.subagents/t-<safe id>.json`.
- The runner (`src/marim_harness/subagents/runner.py`) saves sidecars only at completion:
  `_save_transcript` at runner.py:473, called at lines 770 (foreground), 817 (background),
  863-868 (CLI + children).
- `ProcessHistory` capabilities take a sync `list -> list` function and run before **every**
  model request (`build()` at runner.py:380-392 already registers two).
- `_resumable_history` (runner.py:115) already repairs a captured transcript
  (drop nameless calls + synthesize returns for unanswered ones) — reuse it for resume.
- `JobRegistry` (`src/marim_harness/jobs.py`) is in-memory; `Job` has no `stream_id`.
  `register()` is called with agent spawns at `tools/provider.py:776,782` and bash at `:887`.
- `SessionController.persist` (`session/ctrl.py:206`) skips the write when
  `history_version` is unchanged — a background job settling does NOT bump it, so the
  runner's post-background `session.persist()` must become `persist(force=True)`.
- `replay_history` (`interfaces/tui/session_view.py:84`) builds cards only for
  `spawn_agent` calls without `background=True`, and **never appends them to
  `app.stream.subagents`** — so today the ctrl+x screen is empty after a resume even
  though cards render in the log. Only the live path (`mount_spawn_widget`,
  stream_render.py:701) appends.
- Live sub-agent events route via `app.stream.tool_widgets[stream_id]`
  (stream_render.py:765); detached cards settle via `_detached_cards[job_id]`
  (stream_render.py:469-505).
- `_detached_job_id(content)` (stream_render.py:85) parses a job id out of a detach handoff.
- The main-history repair stub text is `_INTERRUPTED_TOOL_NOTE`
  (`runtime/controller.py:174`): contains `"interrupted before completion"`.
- `workspace/worktree.py` has `_branch_exists` (private) and `create_or_reuse_worktree`
  (reuses an existing branch's worktree, re-adding it if pruned).
- Test fixtures: `tests/conftest.py` has `_make_deps(tmp_path)`, `_make_harness(model, deps,
  store=...)`, `_text_model()`. `tests/test_subagent_transcript_capture.py` shows the
  `SessionStore` + `harness.subagents.run(...)` pattern.

---

### Task 1: TranscriptStore v2 envelope

**Files:**
- Modify: `src/marim_harness/session/transcripts.py`
- Test: `tests/test_transcript_store.py` (exists — append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `TranscriptStore.write(stream_id, messages, cap, meta: dict | None = None)`,
  `TranscriptStore.read(stream_id) -> list | None` (v1 and v2 files),
  `TranscriptStore.read_meta(stream_id) -> dict | None`,
  `TranscriptStore.scan_meta() -> dict[str, dict]` (stream_id → meta; meta must contain
  `"stream_id"` because the filename is a lossy sanitization).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_transcript_store.py` (reuse the file's existing message-building
helpers if it has them; otherwise these are self-contained):

```python
from pydantic_ai.messages import ModelRequest, UserPromptPart

from marim_harness.session import TranscriptStore


def _msgs():
    return [ModelRequest(parts=[UserPromptPart(content="hi")])]


def _meta(sid: str, status: str = "running") -> dict:
    return {"stream_id": sid, "type": "general", "task": "t", "status": status,
            "model": None, "mcp": None, "depth": 1, "max_output_chars": None,
            "isolation": None}


def test_v2_envelope_round_trip(tmp_path):
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg1", _msgs(), 2000, meta=_meta("sg1"))
    assert ts.read("sg1") is not None            # messages come back
    meta = ts.read_meta("sg1")
    assert meta is not None and meta["status"] == "running"


def test_v1_bare_list_still_reads_and_has_no_meta(tmp_path):
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg1", _msgs(), 2000)               # no meta → v1 bare list on disk
    import json
    raw = json.loads((ts._dir / "t-sg1.json").read_text())
    assert isinstance(raw, list)                 # on-disk format unchanged for v1
    assert ts.read("sg1") is not None
    assert ts.read_meta("sg1") is None


def test_scan_meta_maps_stream_ids_and_skips_junk(tmp_path):
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg1", _msgs(), 2000, meta=_meta("sg1"))
    ts.write("sg2", _msgs(), 2000, meta=_meta("sg2", "finished"))
    ts.write("sg3", _msgs(), 2000)               # v1: no meta → not scanned
    (ts._dir / "t-corrupt.json").write_text("{not json")
    metas = ts.scan_meta()
    assert set(metas) == {"sg1", "sg2"}
    assert metas["sg1"]["status"] == "running"


def test_write_stamps_updated_timestamp(tmp_path):
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg1", _msgs(), 2000, meta=_meta("sg1"))
    assert ts.read_meta("sg1")["updated"]        # non-empty ISO stamp
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_transcript_store.py -v`
Expected: the four new tests FAIL with `TypeError: write() got an unexpected keyword argument 'meta'` / `AttributeError: ... no attribute 'read_meta'`.

- [ ] **Step 3: Implement the envelope**

In `src/marim_harness/session/transcripts.py`, add `from datetime import datetime, timezone`
to the imports, then replace `write`/`read` and add the two readers:

```python
    def write(self, stream_id: str, messages: list, cap: int,
              meta: dict | None = None) -> None:
        """Persist one spawn's transcript. With ``meta`` the file is a v2 envelope
        ``{"v": 2, "meta": ..., "messages": [...]}`` — the meta carries what a
        resumed session needs to rebuild the card and (for an interrupted spawn)
        re-run it. Without ``meta`` the historical v1 bare-list format is kept, so
        callers migrate incrementally and old files stay valid. ``meta`` must carry
        ``stream_id``: the filename is a lossy sanitization, so ``scan_meta`` can
        only key results off the id stored inside the file."""
        if not stream_id or not messages:
            return
        try:
            capped = cap_transcript(messages, cap)
            msgs = ModelMessagesTypeAdapter.dump_python(capped, mode="json")
            if meta is None:
                payload = msgs
            else:
                meta = dict(meta)  # never mutate the caller's (reused) meta dict
                meta["stream_id"] = stream_id
                meta["updated"] = datetime.now(timezone.utc).isoformat()
                payload = {"v": 2, "meta": meta, "messages": msgs}
            self._dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._file(stream_id), json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("Failed to write sub-agent transcript %s: %s", stream_id, exc)

    def read(self, stream_id: str) -> list | None:
        path = self._file(stream_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):  # v2 envelope; a bare list is a v1 file
                raw = raw.get("messages", [])
            return list(ModelMessagesTypeAdapter.validate_python(raw))
        except Exception as exc:  # noqa: BLE001 - a corrupt sidecar must not crash resume
            logger.warning("Failed to read sub-agent transcript %s: %s", stream_id, exc)
            return None

    def read_meta(self, stream_id: str) -> dict | None:
        """The v2 meta for one spawn, without validating its messages (cheap).
        None for a missing, corrupt, or v1 (bare-list) sidecar."""
        path = self._file(stream_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read sidecar meta %s: %s", stream_id, exc)
            return None
        if isinstance(raw, dict) and isinstance(raw.get("meta"), dict):
            return raw["meta"]
        return None

    def scan_meta(self) -> dict[str, dict]:
        """stream_id → meta for every v2 sidecar in this session's dir. Used once
        at session resume to find spawns that died mid-run (meta still says
        ``running``). Corrupt and v1 files are skipped with a warning — detection
        degrades to fewer interrupted cards, never a crash."""
        out: dict[str, dict] = {}
        if not self._dir.exists():
            return out
        for path in self._dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text())
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping unreadable sidecar %s: %s", path, exc)
                continue
            meta = raw.get("meta") if isinstance(raw, dict) else None
            sid = meta.get("stream_id") if isinstance(meta, dict) else None
            if sid:
                out[str(sid)] = meta
        return out
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest --no-cov tests/test_transcript_store.py -v`
Expected: ALL PASS (new and pre-existing).

- [ ] **Step 5: Lint, type-check, full suite, commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
git add src/marim_harness/session/transcripts.py tests/test_transcript_store.py
git commit -m "feat(session): v2 transcript sidecar envelope with per-spawn meta"
```

---

### Task 2: Runner meta + per-response checkpointing

**Files:**
- Modify: `src/marim_harness/subagents/runner.py`
- Modify: `docs/superpowers/specs/2026-07-03-subagent-resume-design.md` (the CLI-children sentence, see plan header)
- Test: `tests/test_subagent_resume.py` (create)

**Interfaces:**
- Consumes: `TranscriptStore.write(..., meta=)` from Task 1.
- Produces: `_SpawnPrep.meta: dict | None`; `build(..., checkpoint: Callable[[list], None] | None = None)`;
  `_save_transcript(self, stream_id, messages, meta: dict | None = None)`;
  meta dict shape (Tasks 4–5 read these exact keys):
  `{"stream_id", "type", "task", "model", "mcp", "depth", "max_output_chars", "isolation", "status", "usage", "updated"}`
  where `status ∈ {"running", "finished", "failed"}`, `isolation` is the branch name or None,
  `mcp` is the granted server-name list or None, `usage` is `{"input": int, "output": int}` (final writes only).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_subagent_resume.py`:

```python
"""Sidecar checkpointing and interrupted-spawn resume.

A spawn used to write its transcript sidecar only at completion, so a process
death mid-run lost the transcript entirely. The runner now flushes a v2 envelope
(meta + messages) before every model request via a ProcessHistory capability and
finalizes it with a terminal status, so a crashed spawn leaves a resumable trail.
"""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.session import SessionStore, TranscriptStore
from tests.conftest import _make_deps, _make_harness


def _session_store(tmp_path: Path) -> SessionStore:
    return SessionStore(
        path=tmp_path / "sessions" / "test.json", workspace_root=tmp_path,
        session_id="test-session", name="test",
    )


def _tool_then_text_model() -> FunctionModel:
    """First request: call list_files. Second: final report."""
    def fn(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="list_files", args={"path": "."}, tool_call_id="t1")])
        return ModelResponse(parts=[TextPart(content="report")])
    return FunctionModel(fn)


def _spy_saves(runner):
    """Record every _save_transcript meta status, preserving behavior."""
    seen: list[str | None] = []
    orig = runner._save_transcript

    def spy(stream_id, messages, meta=None):
        seen.append(None if meta is None else meta.get("status"))
        orig(stream_id, messages, meta=meta)

    runner._save_transcript = spy
    return seen


@pytest.mark.anyio
async def test_spawn_checkpoints_running_then_finalizes(tmp_path):
    store = _session_store(tmp_path)
    harness = _make_harness(_tool_then_text_model(), _make_deps(tmp_path), store=store)
    seen = _spy_saves(harness.subagents)
    out = await harness.subagents.run("general", "look around", stream_id="sg-ck")
    assert out == "report"
    # At least one mid-run checkpoint (per model request) plus the final write.
    assert "running" in seen and seen[-1] == "finished"
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-ck")
    assert meta["status"] == "finished"
    assert meta["type"] == "general" and meta["task"] == "look around"
    assert meta["depth"] == 1 and meta["usage"]["output"] >= 0


@pytest.mark.anyio
async def test_failed_spawn_leaves_sidecar_marked_running(tmp_path):
    """A spawn that dies mid-run gets no final write — its sidecar stays
    status=running, which is exactly what the resume scan treats as interrupted."""
    def fn(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="list_files", args={"path": "."}, tool_call_id="t1")])
        raise RuntimeError("boom")  # permanent → no retry, spawn fails

    store = _session_store(tmp_path)
    harness = _make_harness(FunctionModel(fn), _make_deps(tmp_path), store=store)
    out = await harness.subagents.run("general", "task", stream_id="sg-dead")
    assert "failed" in out  # foreground contains the crash as an error string
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-dead")
    assert meta is not None and meta["status"] == "running"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_resume.py -v`
Expected: FAIL — `_save_transcript` takes no `meta` kwarg / `read_meta` returns None.

- [ ] **Step 3: Implement checkpointing in the runner**

All edits in `src/marim_harness/subagents/runner.py`.

3a. `_SpawnPrep` gains the meta field:

```python
    first_event_at: list[float]  # mutable; ``on_first_event`` probe appends during run
    depth: int  # depth of the spawned sub-agent
    meta: dict | None = None  # sidecar meta template (Task: subagent resume)
```

3b. `build()` gains a `checkpoint` parameter (add to the signature after `mask_trigger`):

```python
        checkpoint: Callable[[list], None] | None = None,
```

and, in the capabilities block, register it FIRST — before `_drop_nameless_tool_calls` —
so the persisted history is the raw conversation (the read-side repair in
`_resumable_history` re-runs the same scrubs on resume anyway):

```python
        capabilities: list[ProcessHistory[Deps]] = []
        if checkpoint is not None:
            # Sidecar checkpoint: ProcessHistory runs before EVERY model request,
            # which is exactly the per-model-response boundary the resume design
            # wants — each checkpoint ends at a message boundary. The processor
            # must return the history unchanged; the write is a side effect.
            def _checkpoint_history(messages: list) -> list:
                checkpoint(messages)
                return messages

            capabilities.append(ProcessHistory(_checkpoint_history))
        capabilities.append(ProcessHistory(_drop_nameless_tool_calls))
```

(the existing `capabilities` initialization with `_drop_nameless_tool_calls` inline is
replaced by the above).

3c. `_prepare_spawn` builds the meta and the checkpoint closure. Before the
`self.build(...)` call insert:

```python
        meta: dict | None = None
        checkpoint = None
        if stream_id:
            # The sidecar meta template: everything a resumed session needs to
            # rebuild the card (type/task/model) and re-run the spawn
            # (mcp/depth/isolation/max_output_chars). Status stays "running"
            # for every mid-run checkpoint; the final save stamps the terminal
            # status. parent_id is deliberately absent — the runner doesn't know
            # its caller's stream, so a synthesized interrupted card renders
            # top-level.
            meta = {
                "stream_id": stream_id, "type": type, "task": task,
                "model": model, "mcp": mcp_names, "depth": depth,
                "max_output_chars": max_output_chars,
                "isolation": iso["branch"] if iso else None,
                "status": "running",
            }

            def checkpoint(messages: list, _meta=meta) -> None:
                self._save_transcript(stream_id, messages, meta=_meta)
```

then thread it into the build call (`checkpoint=checkpoint`) and into the returned
`_SpawnPrep` (`meta=meta`).

3d. `_save_transcript` accepts and forwards meta:

```python
    def _save_transcript(self, stream_id: str, messages: list,
                         meta: dict | None = None) -> None:
        try:
            store = self._transcript_store()
            if stream_id and messages and store is not None:
                store.write(stream_id, messages, self._transcript_cap, meta=meta)
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("Failed to save transcript %s: %s", stream_id, exc)
```

3e. Add a final-meta helper next to `_save_transcript`:

```python
    def _final_meta(self, prep: _SpawnPrep, status: str, usage) -> dict | None:
        """The terminal sidecar meta for a finished spawn: the prep's template
        stamped with its terminal status and total spend. None when the spawn had
        no stream id (headless) — the sidecar then stays v1."""
        if prep.meta is None:
            return None
        meta = dict(prep.meta)
        meta["status"] = status
        if usage is not None:
            meta["usage"] = {"input": usage.input_tokens, "output": usage.output_tokens}
        return meta
```

3f. Final writes. In `_execute_foreground_spawn` (runner.py:770) and
`_execute_background_spawn` (runner.py:817), replace
`self._save_transcript(stream_id, result.all_messages())` with:

```python
        self._save_transcript(
            stream_id, result.all_messages(),
            meta=self._final_meta(prep, "finished", result.usage),
        )
```

3g. In `_execute_background_spawn`, change `self.session.persist()` to
`self.session.persist(force=True)` and extend its comment:

```python
        # A background spawn finishes off-turn, so no run_turn will fold in its
        # spend — persist right away so the saved session reflects it even if the
        # process exits before the next turn. force=True: the persist cache keys
        # off history_version, which a background completion never bumps, so an
        # unforced persist here would be silently skipped (losing the spend and,
        # since Task 3, the settled-jobs history entry).
        self.session.persist(force=True)
```

3h. In `_execute_cli_spawn` (runner.py:863), give the parent spawn a meta on its
completion-time write (children stay v1 — see the plan header deviation note):

```python
        self._save_transcript(
            stream_id, result.transcript,
            meta={
                "stream_id": stream_id, "type": defn.name, "task": task,
                "model": model, "mcp": None, "depth": 1,
                "max_output_chars": max_output_chars,
                "isolation": iso["branch"] if iso else None,
                "status": "finished",
                "usage": {"input": result.usage.input_tokens,
                          "output": result.usage.output_tokens},
            },
        )
```

and its background `self.session.persist()` → `self.session.persist(force=True)`.

3i. In the spec, replace the sentence "they gain meta on that final write so they replay
with full card state. A CLI child interrupted mid-run stays lost" with: "the parent CLI
spawn gains meta on that final write; children stay v1 (their card state replays from the
parent transcript, and `child_transcripts()` carries no type/task to build meta from). A
CLI child interrupted mid-run stays lost".

- [ ] **Step 4: Run the tests**

Run: `uv run pytest --no-cov tests/test_subagent_resume.py tests/test_subagent_transcript_capture.py tests/test_subagent_retry.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, full suite, commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
git add src/marim_harness/subagents/runner.py tests/test_subagent_resume.py docs/superpowers/specs/2026-07-03-subagent-resume-design.md
git commit -m "feat(subagents): checkpoint sidecar envelope per model response"
```

---

### Task 3: Finished-job history in the session payload

**Files:**
- Modify: `src/marim_harness/jobs.py`
- Modify: `src/marim_harness/session/store.py`
- Modify: `src/marim_harness/session/ctrl.py`
- Modify: `src/marim_harness/tools/provider.py` (agent-spawn `register` calls pass `stream_id`)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`_render_jobs` includes history)
- Test: `tests/test_jobs.py`, `tests/test_session.py` (append to both)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Job.stream_id: str | None`, `Job.finished_at: str | None`;
  `JobRegistry.history: list[Job]` (read-only imported summaries, never in `_jobs`);
  `JobRegistry.register(..., stream_id: str | None = None)`;
  `JobRegistry.export_settled() -> list[dict]` (entries:
  `{"id", "kind", "label", "status", "result_tail", "stream_id", "finished_at"}`, capped at 50);
  `JobRegistry.import_history(entries: list[dict]) -> None`;
  `SessionStore.save(..., jobs: list | None = None)`;
  `SessionStore.load() -> tuple[list, RunUsage, list, float | None, list]` (5-tuple — the
  new last element is the jobs history; **update every `load()` caller**).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jobs.py` (mirror its existing async patterns):

```python
@pytest.mark.anyio
async def test_export_and_import_settled_history():
    reg = JobRegistry()

    async def _ok() -> str:
        return "did the thing " * 40  # long → tail-capped on export

    jid = reg.register("agent", "general: do it", _ok(), stream_id="sg-1")
    await reg.wait(jid)
    exported = reg.export_settled()
    assert len(exported) == 1
    entry = exported[0]
    assert entry["id"] == jid and entry["stream_id"] == "sg-1"
    assert entry["status"] == "done" and len(entry["result_tail"]) <= 210
    assert entry["finished_at"]

    fresh = JobRegistry()
    fresh.import_history(exported)
    assert [j.id for j in fresh.history] == [jid]
    assert fresh.history[0].stream_id == "sg-1"
    # Imported ids seed the counter so a new job never collides with history.
    assert fresh.register("bash", "x", _noop()) != jid


async def _noop() -> str:
    return ""


def test_import_history_is_not_live():
    reg = JobRegistry()
    reg.import_history([{"id": "job-1", "kind": "agent", "label": "l",
                         "status": "done", "result_tail": "r",
                         "stream_id": "sg", "finished_at": "t"}])
    assert reg.get("job-1") is None          # not pollable/killable
    assert not reg.has_finished_pending()    # never enters the digest
    reg.clear_history()
    assert reg.history == []
```

Append to `tests/test_session.py`:

```python
def test_session_store_round_trips_jobs_history(tmp_path):
    store = SessionStore(path=tmp_path / "s.json", workspace_root=tmp_path,
                         session_id="sid", name="s")
    entry = {"id": "job-1", "kind": "agent", "label": "general: x",
             "status": "done", "result_tail": "ok", "stream_id": "sg-1",
             "finished_at": "2026-07-03T00:00:00+00:00"}
    store.save([], RunUsage(), jobs=[entry])
    *_, jobs = store.load()
    assert jobs == [entry]


def test_session_store_without_jobs_key_loads_empty(tmp_path):
    store = SessionStore(path=tmp_path / "s.json", workspace_root=tmp_path,
                         session_id="sid", name="s")
    store.save([], RunUsage())      # no jobs kwarg — old-style file
    *_, jobs = store.load()
    assert jobs == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_jobs.py tests/test_session.py -v`
Expected: new tests FAIL (`unexpected keyword argument 'stream_id'` / `'jobs'`, no
`export_settled`); pre-existing ones pass.

- [ ] **Step 3: Implement the registry side**

In `src/marim_harness/jobs.py`: add `import re` and a datetime import
(`from datetime import datetime, timezone`); extend `Job`:

```python
    id: str
    kind: str  # "bash" | "agent"
    label: str
    status: Status = "running"
    result: str | None = None
    # The spawn's tool_call_id when kind == "agent" — the cross-cutting key that
    # joins a settled job back to its sub-agent card and transcript sidecar.
    stream_id: str | None = None
    # UTC ISO stamp set at settle time; rides into the persisted history.
    finished_at: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    kill: Callable[[], None] | None = field(default=None, repr=False)
    output_fn: Callable[[], str] | None = field(default=None, repr=False)
```

In `JobRegistry.__init__` add:

```python
        # Settled-job summaries imported from the persisted session (spec
        # 2026-07-03-subagent-resume, §2). Read-only display state: never in
        # ``_jobs``, never killable/pollable, never in the digest — a prior
        # process already surfaced these results.
        self.history: list[Job] = []
```

In `_settle`, stamp the finish time (first line after the running check):

```python
        job.finished_at = datetime.now(timezone.utc).isoformat()
```

`register` gains the kwarg and passes it through:

```python
    def register(self, kind: str, label: str, coro: Awaitable[str], *,
                 kill: Callable[[], None] | None = None,
                 output_fn: Callable[[], str] | None = None,
                 stream_id: str | None = None) -> str:
        ...
        job = Job(id=self._next_id(), kind=kind, label=label,
                  kill=kill, output_fn=output_fn, stream_id=stream_id)
```

Add near `_digest_tail` (extract its tail logic so both share it):

```python
_HISTORY_CAP = 50


def _result_tail(result: str | None) -> str:
    """The last _DIGEST_RESULT_CHARS chars of a result, whitespace-collapsed —
    the same verdict-carrying tail the digest inlines."""
    if not result:
        return ""
    compact = " ".join(result.split())
    if len(compact) > _DIGEST_RESULT_CHARS:
        compact = "…" + compact[-_DIGEST_RESULT_CHARS:]
    return compact
```

(and rewrite `_digest_tail` as `tail = _result_tail(job.result); return f": {tail}" if tail else ""`).

Add the export/import pair to `JobRegistry`:

```python
    def export_settled(self) -> list[dict]:
        """Summaries of every terminal job — prior-session history first, then
        this process's settles — capped to the newest _HISTORY_CAP so a
        long-lived session doesn't accrete unboundedly. Results are persisted as
        tails, not full reports: the session payload must not balloon (full
        reports were already delivered via the digest or spill files)."""
        def entry(j: Job) -> dict:
            return {"id": j.id, "kind": j.kind, "label": j.label,
                    "status": j.status, "result_tail": _result_tail(j.result),
                    "stream_id": j.stream_id, "finished_at": j.finished_at}

        settled = [entry(j) for j in self._jobs.values() if j.status != "running"]
        prior = [entry(j) for j in self.history]
        return (prior + settled)[-_HISTORY_CAP:]

    def import_history(self, entries: list[dict]) -> None:
        """Load prior-session settled summaries as read-only ``history``. Also
        seeds the id counter past any imported ``job-N`` so a job launched this
        process never shares an id with a history row on the panel."""
        self.history = [
            Job(id=str(e.get("id", "?")), kind=str(e.get("kind", "agent")),
                label=str(e.get("label", "")), status=e.get("status", "done"),
                result=e.get("result_tail") or None,
                stream_id=e.get("stream_id"), finished_at=e.get("finished_at"))
            for e in entries
            if isinstance(e, dict)
        ]
        for job in self.history:
            m = re.fullmatch(r"job-(\d+)", job.id)
            if m:
                self._counter = max(self._counter, int(m.group(1)))
        self._notify()
```

`clear_history` also drops it (`self.history = []` alongside the `_jobs` filter).

- [ ] **Step 4: Implement the store + controller side**

`src/marim_harness/session/store.py` — `save` gains `jobs: list | None = None` and writes
`payload["jobs"] = jobs or []` (after `"tasks"`); `load` returns the 5-tuple:

```python
        return (messages, usage, data.get("tasks", []),
                data.get("duration_seconds"), data.get("jobs", []))
```

Grep every `store.load()` / `.load()` caller and unpack the fifth element:

Run: `grep -rn "\.load()" src tests | grep -v "tasks.load\|deps.tasks"`

In `src/marim_harness/session/ctrl.py`:
- `persist` passes the export:

```python
            self.store.save(
                self.history, self.usage, self.deps.tasks.to_payload(),
                duration_seconds=self.duration_seconds + elapsed,
                jobs=self.deps.jobs.export_settled(),
            )
```

- `_load_active_store` imports it:

```python
        self.history, self.usage, tasks, prev_duration, jobs = self.store.load()
        self.deps.tasks.load(tasks)
        self.deps.jobs.import_history(jobs)
```

`SessionController` reaches the registry through `self.deps.jobs` (already threaded).

In `src/marim_harness/tools/provider.py`, the two agent `register` calls in `spawn_agent`
(lines 776 and 782) gain `stream_id=ctx.tool_call_id or None` as a kwarg.

In `src/marim_harness/interfaces/tui/app.py`, `_render_jobs` shows history before live:

```python
        panel.show_jobs(self.jobs.history + self.jobs.list())
```

(history rows are terminal, so `render_jobs` already suffixes them `(done)`/`(failed)`).

- [ ] **Step 5: Run the tests**

Run: `uv run pytest --no-cov tests/test_jobs.py tests/test_session.py tests/test_jobs_tools.py -v`
Expected: PASS. Fix any `load()` caller the grep surfaced (tests included).

- [ ] **Step 6: Lint, type-check, full suite, commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
git add src/marim_harness/jobs.py src/marim_harness/session/store.py src/marim_harness/session/ctrl.py src/marim_harness/tools/provider.py src/marim_harness/interfaces/tui/app.py tests/
git commit -m "feat(jobs): persist settled-job history in the session payload"
```

---

### Task 4: Replay every spawn as a card; detect interrupted spawns

**Files:**
- Modify: `src/marim_harness/interfaces/tui/session_view.py`
- Modify: `src/marim_harness/interfaces/tui/subagents_viewer.py` (`_load_transcript` passes parent id)
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent.py` (interrupted status)
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent_stats.py` (aggregate bucket)
- Test: `tests/test_session_view_replay.py` (append, mirroring its existing app/pilot fixtures)

**Interfaces:**
- Consumes: `scan_meta` (Task 1), `JobRegistry.history` + `Job.stream_id` (Task 3),
  meta keys from Task 2.
- Produces: replayed cards registered in `app.stream.subagents` (with `parent_id` for
  nested pane replays); `SubAgentWidget.status` gains `"interrupted"`;
  `SessionView.finish_replayed_cards()` (called from `render_session` after replay).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session_view_replay.py`, following that file's existing pattern for
building a history and driving `render_session`/`replay_history` (reuse its helpers for
constructing `ModelRequest`/`ModelResponse` pairs and the app fixture). The behaviors to
pin, each as its own test:

```python
# 1. A background spawn_agent call replays as a SubAgentWidget card, not a plain
#    tool row, and the card lands in app.stream.subagents:
#    history: spawn_agent ToolCallPart(args={"type": "general", "task": "t",
#    "background": True}, tool_call_id="sg-bg") + ToolReturnPart(content="Started
#    job-3 (agent) — general: t"). After render_session:
#    assert any(w.stream_id == "sg-bg" for w in app.stream.subagents)
#    card = next(w for w in app.stream.subagents if w.stream_id == "sg-bg")
#    assert card.detached and card.job_id == "job-3"

# 2. The jobs history supplies the settled status/report:
#    seed app.harness.deps.jobs.import_history([{"id": "job-3", "kind": "agent",
#    "label": "general: t", "status": "done", "result_tail": "all good",
#    "stream_id": "sg-bg", "finished_at": "t"}]) BEFORE render_session.
#    assert card.status == "done" and card.report == "all good"

# 3. A sidecar left status=running flips its card to interrupted:
#    write a v2 sidecar for the foreground spawn's stream_id with
#    meta status "running" (TranscriptStore(store.path, store.session_id)
#    .write(...)), give the spawn's ToolReturnPart the repair-stub text
#    ("Tool call was interrupted before completion and did not run (the turn "
#    "was aborted). Re-issue it if you still need the result.").
#    After render_session: assert card.status == "interrupted"

# 4. A running sidecar with NO card in the history synthesizes one:
#    write a v2 sidecar for stream_id "sg-ghost" (status running) with no
#    matching spawn in the history. After render_session:
#    ghost = next(w for w in app.stream.subagents if w.stream_id == "sg-ghost")
#    assert ghost.status == "interrupted" and ghost.pane is not None
#    assert ghost.pane.transcript_loaded is False   # lazy-load still applies

# 5. A replayed FOREGROUND card also joins app.stream.subagents (regression for
#    the empty ctrl+x screen after resume).
```

Write these as real tests with the file's fixtures — the comments above are the
assertions each must make, not placeholders to leave in.

Also append to `tests/test_transcript_cap.py` or a widget test file if one exists for
`SubAgentWidget` (grep: `grep -rln "SubAgentWidget" tests/`):

```python
def test_interrupted_status_glyph_and_activity():
    w = SubAgentWidget("general", "task text")
    w.finish("", status="interrupted")
    assert w._glyph() == "⏸"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_session_view_replay.py -v`
Expected: FAIL (background spawns replay as tool rows; `subagents` list empty).

- [ ] **Step 3: Widget support for `interrupted`**

`src/marim_harness/interfaces/tui/widgets/subagent.py`:
- Update the status comment: `self.status = "pending"  # "pending" | "done" | "denied" | "failed" | "interrupted"`.
- `_glyph`:

```python
        if self.status == "interrupted":
            return "⏸"
```

(insert before the `waiting` check).
- `_paint_activity` — add an arm before the final `else`:

```python
        elif self.status == "interrupted":
            self._activity.update(Content.assemble(
                ("↳ interrupted — press r in the sub-agents screen (ctrl+x) to resume",
                 "dim"),
            ))
```

`src/marim_harness/interfaces/tui/widgets/subagent_stats.py` — `aggregate` counts
interrupted with failed (it's terminal and needs attention; a dedicated summary bucket
is not worth a bar-format change):

```python
        elif a.status in ("failed", "denied", "interrupted"):
            failed += 1
```

- [ ] **Step 4: Replay changes**

All in `src/marim_harness/interfaces/tui/session_view.py`.

4a. `_replay_parts` gains a `parent_id: str | None = None` keyword parameter. In the
`ToolCallPart` arm, drop the background exclusion and register the card:

```python
            # Every spawn_agent call rebuilds as its SubAgentWidget card
            # (mirroring the live path) rather than a generic tool row —
            # foreground AND background, so the sub-agents screen repopulates
            # after a resume. The card also joins the renderer's backing list,
            # which the live path does in mount_spawn_widget; replay skipped it
            # historically, leaving the ctrl+x screen empty on a resumed session.
            if part.tool_name == "spawn_agent":
                group = None
                solo = None
                widget = SubAgentWidget(
                    str(args.get("type", "")),
                    str(args.get("task", "")),
                    str(args.get("model") or ""),
                    description=str(args.get("description") or ""),
                )
                widget.stream_id = part.tool_call_id
                widget.parent_id = parent_id
                if all(w.stream_id != widget.stream_id
                       for w in self.app.stream.subagents):
                    self.app.stream.subagents.append(widget)
                tool_widgets[part.tool_call_id] = widget
                await mount_fn(widget)
```

4b. In the `ToolReturnPart` arm, intercept detach handoffs so a background card waits
for the jobs-history join instead of finishing on the handoff text:

```python
        elif isinstance(part, ToolReturnPart):
            widget = tool_widgets.get(part.tool_call_id)
            if widget is not None:
                content = str(part.content)
                if isinstance(widget, SubAgentWidget):
                    from .stream_render import _detached_job_id
                    job_id = _detached_job_id(content)
                    if job_id is not None:
                        # A detach handoff is a job-id receipt, not the report —
                        # finish_replayed_cards joins the real outcome from the
                        # persisted jobs history / sidecar meta after replay.
                        widget.detached = True
                        widget.job_id = job_id
                        return group, solo
                status = status_from_part(part)
                ...  # existing failed-spawn detection + widget.finish unchanged
```

(`"Started job-N (agent) — label"` from an explicit `background=True` spawn must also
match — check `_detached_job_id`'s regex in stream_render.py:85 and, if it only matches
the auto-detach wording `"Started detached sub-agent job-N"`, extend it to also match
`"Started job-N (agent)"`.)

4c. `replay_history`'s main-log-only post-processing block (pane creation +
model-label fallback) drops its `and not part.args_as_dict().get("background")`
condition so background cards get panes too.

4d. `replay_messages_into` gains `parent_id: str | None = None` and threads it into
`_replay_parts`; `subagents_viewer._load_transcript` passes the owning card's id:

```python
            await self.app.session.replay_messages_into(pane, msgs, parent_id=stream_id)
```

4e. Add the post-replay join + interrupted scan to `SessionView`:

```python
    _REPAIR_STUB_MARKER = "interrupted before completion"

    async def finish_replayed_cards(self) -> None:
        """Settle every replayed card's final state from the persisted record:
        the jobs history supplies a background spawn's status/report (its
        ToolReturnPart is only a job-id handoff), and the sidecar meta scan flags
        spawns that died mid-run as interrupted — including ones whose owning
        turn never persisted, which get a card synthesized from meta alone so no
        work silently vanishes."""
        store = self.app.harness.session.store
        if store is None:
            return
        from ...session import TranscriptStore
        metas = TranscriptStore(store.path, store.session_id).scan_meta()
        jobs = self.app.harness.deps.jobs
        settled = {j.stream_id: j for j in jobs.history if j.stream_id}
        for card in list(self.app.stream.subagents):
            job = settled.get(card.stream_id)
            meta = metas.get(card.stream_id)
            meta_status = meta.get("status") if meta else None
            if card.status == "pending":
                # A detached card whose handoff we skipped in _replay_parts.
                if job is not None:
                    status = "failed" if job.status in ("failed", "cancelled") else "done"
                    card.finish(job.result or "", status=status)
                elif meta_status == "finished":
                    card.finish("", status="done")
                elif meta_status == "failed":
                    card.finish("", status="failed")
                else:
                    card.finish("", status="interrupted")
            elif (meta_status == "running" and job is None
                  and self._REPAIR_STUB_MARKER in card.report):
                # A foreground spawn cut down mid-run: the main history's repair
                # stub finished the card "done", but the sidecar (whose final
                # write never happened) knows it never completed.
                card.finish(card.report, status="interrupted")
        # Spawns with a running sidecar but no card at all: the owning turn was
        # never persisted (crash before the turn's persist). Synthesize a card
        # from meta so the work is discoverable and resumable.
        have = {w.stream_id for w in self.app.stream.subagents}
        log = self.app.query_one("#log", VerticalScroll)
        for sid, meta in metas.items():
            if meta.get("status") != "running" or sid in have or sid in settled:
                continue
            widget = SubAgentWidget(
                str(meta.get("type", "")), str(meta.get("task", "")),
                str(meta.get("model") or self.app.harness.model_label or ""),
            )
            widget.stream_id = sid
            self.app.stream.subagents.append(widget)
            await log.mount(widget)
            host = self.app.query_one(SubAgentDetailHost)
            pane = host.add_pane(sid, widget.agent_type, widget.model_label,
                                 widget.display_title(), widget.agent_task)
            widget.pane = pane  # transcript_loaded stays False → lazy sidecar load
            widget.finish("", status="interrupted")
```

4f. Call it from `render_session`, right after the replay:

```python
            if self.app.harness.session.history:
                await self.replay_history(log)
            await self.finish_replayed_cards()
```

(placed inside the `try` so the rebuild guard still covers it; it must also run when the
history is empty — a crash can leave sidecars with no persisted turn).

- [ ] **Step 5: Run the tests**

Run: `uv run pytest --no-cov tests/test_session_view_replay.py tests/test_app.py -v`
Expected: PASS.

- [ ] **Step 6: Lint, type-check, full suite, commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
git add src/marim_harness/interfaces/tui tests/
git commit -m "feat(tui): replay every spawn as a card; flag interrupted spawns"
```

---

### Task 5: `resume_spawn` + the sub-agents screen action

**Files:**
- Modify: `src/marim_harness/subagents/runner.py`
- Modify: `src/marim_harness/workspace/worktree.py` (public `branch_exists`)
- Modify: `src/marim_harness/runtime/deps.py` (`HarnessServices.resume_subagent`)
- Modify: `src/marim_harness/runtime/harness.py` (wire the service)
- Modify: `src/marim_harness/interfaces/tui/widgets/subagents_view.py` (binding + action)
- Modify: `src/marim_harness/interfaces/tui/subagents_viewer.py` (`resume_selected`)
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (`adopt_resumed_card`)
- Test: `tests/test_subagent_resume.py` (append)

**Interfaces:**
- Consumes: envelope + meta keys (Tasks 1–2), `register(..., stream_id=)` (Task 3),
  `"interrupted"` card status (Task 4).
- Produces: `SubagentRunner.resume_spawn(stream_id: str) -> tuple[str | None, str]`
  (`(job_id, message)` on success, `(None, reason)` on refusal);
  `_run_to_completion(..., history: list | None = None)`;
  `_execute_background_spawn(..., history: list | None = None)`;
  `branch_exists(repo_root, branch) -> bool` (public);
  `HarnessServices.resume_subagent` (async, same signature as `resume_spawn`);
  `StreamRenderer.adopt_resumed_card(card, job_id)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_resume.py`:

```python
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

from marim_harness.session import TranscriptStore


def _interrupted_meta(sid: str) -> dict:
    return {"stream_id": sid, "type": "general", "task": "original task",
            "model": None, "mcp": None, "depth": 1, "max_output_chars": None,
            "isolation": None, "status": "running"}


def _dangling_history() -> list:
    """A transcript that died mid-tool-call — the resume must repair it."""
    return [
        ModelRequest(parts=[UserPromptPart(content="original task")]),
        ModelResponse(parts=[ToolCallPart(
            tool_name="read_file", args={"path": "x"}, tool_call_id="dangling")]),
    ]


def _resume_model() -> FunctionModel:
    """Asserts the incoming history was repaired (the dangling call has a
    synthesized return), then finishes."""
    def fn(messages, info):
        returns = [p for m in messages for p in getattr(m, "parts", [])
                   if isinstance(p, ToolReturnPart)]
        assert any(p.tool_call_id == "dangling" for p in returns), \
            "resume must synthesize a return for the dangling tool call"
        return ModelResponse(parts=[TextPart(content="resumed-ok")])
    return FunctionModel(fn)


@pytest.mark.anyio
async def test_resume_spawn_repairs_history_and_finishes(tmp_path):
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    ts.write("sg-int", _dangling_history(), 2000, meta=_interrupted_meta("sg-int"))
    job_id, message = await harness.subagents.resume_spawn("sg-int")
    assert job_id is not None, message
    report = await harness.deps.jobs.wait(job_id)
    assert report == "resumed-ok"
    job = harness.deps.jobs.get(job_id)
    assert job is not None and job.stream_id == "sg-int"
    assert ts.read_meta("sg-int")["status"] == "finished"


@pytest.mark.anyio
async def test_resume_refuses_v1_finished_and_double_resume(tmp_path):
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    # v1 sidecar (no meta) → refuse
    ts.write("sg-v1", _dangling_history(), 2000)
    job_id, msg = await harness.subagents.resume_spawn("sg-v1")
    assert job_id is None and "resumable" in msg.lower()
    # finished spawn → refuse
    ts.write("sg-done", _dangling_history(), 2000,
             meta={**_interrupted_meta("sg-done"), "status": "finished"})
    job_id, msg = await harness.subagents.resume_spawn("sg-done")
    assert job_id is None
    # already resuming → refuse the second call
    ts.write("sg-int", _dangling_history(), 2000, meta=_interrupted_meta("sg-int"))
    first, _ = await harness.subagents.resume_spawn("sg-int")
    assert first is not None
    second, msg = await harness.subagents.resume_spawn("sg-int")
    assert second is None and first in msg
    await harness.deps.jobs.wait(first)


@pytest.mark.anyio
async def test_resume_refuses_when_isolation_branch_is_gone(tmp_path):
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    meta = {**_interrupted_meta("sg-iso"), "isolation": "subagent/gone"}
    ts.write("sg-iso", _dangling_history(), 2000, meta=meta)
    job_id, msg = await harness.subagents.resume_spawn("sg-iso")
    assert job_id is None and "subagent/gone" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_resume.py -v`
Expected: new tests FAIL with `AttributeError: ... no attribute 'resume_spawn'`.

- [ ] **Step 3: Worktree helper**

In `src/marim_harness/workspace/worktree.py`, rename `_branch_exists` to
`branch_exists` and update its two-or-so internal call sites
(`grep -n "_branch_exists" src/marim_harness/workspace/worktree.py`). Check
`src/marim_harness/workspace/__init__.py` re-exports if other worktree names are
exported there; export `branch_exists` alongside them if so.

- [ ] **Step 4: Runner implementation**

All in `src/marim_harness/subagents/runner.py`.

4a. `_run_to_completion` gains `history: list | None = None` (after `stream_id`) and the
run call threads it — a resume starts from a persisted transcript exactly the way a
transient-retry resume starts from a captured one:

```python
                with _fresh_capture() as captured:
                    return await sub.run(
                        task if resume_history is None else None,
                        message_history=(resume_history if resume_history is not None
                                         else history),
                        deps=run_deps, toolsets=granted,
                        event_stream_handler=handler,
                        usage=run_usage,
                        usage_limits=UsageLimits(request_limit=self._request_limit),
                    )
```

(note: when `history` is set, the initial call passes BOTH `task` — the continuation
prompt — and `message_history`; pydantic-ai appends the prompt as a new request on top
of the provided history.)

4b. `_execute_background_spawn` gains `history: list | None = None` and threads it into
`_run_to_completion(..., stream_id, history=history)`. No other body change — the whole
tail (hooks stop, final sidecar write, usage, `persist(force=True)`, cap, worktree
close) is exactly what a resumed run needs too.

4c. Add the resume API (place after `run_background`):

```python
    _CONTINUATION_PROMPT = (
        "You were interrupted before finishing. The conversation above is your "
        "own earlier progress on this task — continue from where it leaves off "
        "and finish the task, then report as usual."
    )

    async def resume_spawn(self, stream_id: str) -> tuple[str | None, str]:
        """Continue an interrupted spawn from its persisted sidecar as a
        background job. Returns ``(job_id, message)`` on success or
        ``(None, reason)`` on refusal — the reason is always user-renderable.

        Always background, even for an originally-foreground spawn: its owning
        turn is gone after a restart (the main history's dangling spawn_agent
        call was repaired with a synthetic return), so the finished-job digest
        is the only report consumer that still exists — and it already works."""
        store = self._transcript_store()
        if store is None:
            return None, "No session store — can't resume."
        meta = store.read_meta(stream_id)
        if meta is None:
            return None, ("No resumable transcript for this spawn (missing or "
                          "pre-envelope sidecar).")
        status = meta.get("status")
        if status not in ("running", "interrupted"):
            return None, f"Spawn already {status} — nothing to resume."
        for job in self.deps.jobs.list():
            if job.stream_id == stream_id and job.status == "running":
                return None, f"Already resuming as {job.id}."
        messages = store.read(stream_id)
        history = _resumable_history(messages or [])
        if history is None:
            return None, "Transcript unreadable or empty — can't resume."
        type_ = str(meta.get("type") or "")
        task = str(meta.get("task") or "")
        iso = None
        branch = meta.get("isolation")
        if branch:
            repo = repo_root(self.deps.workspace.root)
            if repo is None or not branch_exists(repo, branch):
                return None, (f"Isolation branch {branch!r} no longer exists — "
                              "can't resume this isolated spawn.")
            try:
                path = create_or_reuse_worktree(repo, branch)
            except WorktreeError as exc:
                return None, f"Couldn't reopen the isolated worktree: {exc}"
            iso = {"repo": repo, "branch": branch, "path": path}
        prep = await self._prepare_spawn(
            type_, task, meta.get("mcp"), meta.get("max_output_chars"),
            meta.get("model"), iso, iso["path"] if iso else None, stream_id,
            debug=logger.isEnabledFor(logging.DEBUG), t0=time.perf_counter(),
            depth=int(meta.get("depth") or 1),
        )
        if isinstance(prep, str):
            if iso:
                self._teardown_worktree(iso)  # keep the branch — it's prior work
            return None, prep
        label = f"{type_}: resumed — {task}"
        job_id = self.deps.jobs.register(
            "agent", label,
            self._execute_background_spawn(
                type_, self._CONTINUATION_PROMPT, stream_id,
                meta.get("max_output_chars"), prep, history=history,
            ),
            stream_id=stream_id,
        )
        return job_id, f"Resumed as {job_id}."
```

Add `branch_exists` to the existing `..workspace.worktree` import block.

One invariant to keep: `_execute_background_spawn`'s failure path calls
`_discard_worktree` (drops the branch). For a *resumed* isolated spawn the branch holds
prior committed work and must survive a failed resume — in `_execute_background_spawn`,
the `except` blocks' worktree handling changes from `self._discard_worktree(prep.iso)` to:

```python
            if prep.iso:
                if history is None:
                    self._discard_worktree(prep.iso)
                else:
                    # A resumed spawn's branch holds prior committed work; a
                    # failed resume must not destroy it. Tear down only the
                    # worktree checkout and keep the branch.
                    self._teardown_worktree(prep.iso, force=True)
```

(in both the `except Exception` and `except BaseException` arms).

- [ ] **Step 5: Service wiring**

`src/marim_harness/runtime/deps.py` — add to `HarnessServices` (import shape matches the
existing `SubAgentRunner`/`BackgroundAgentRunner` aliases; define a matching alias):

```python
ResumeSubagent = Callable[[str], Awaitable[tuple[Optional[str], str]]]
```

(placed with the other callback type aliases in that module, matching their style), then:

```python
    # Lets the sub-agents screen resume an interrupted spawn from its persisted
    # transcript as a background job (spec 2026-07-03-subagent-resume, §4).
    resume_subagent: Optional["ResumeSubagent"] = None
```

`src/marim_harness/runtime/harness.py` — where services are built (the block at
harness.py:157 that sets `run_background_agent=subagents.run_background`), add:

```python
        resume_subagent=subagents.resume_spawn,
```

- [ ] **Step 6: TUI wiring**

`src/marim_harness/interfaces/tui/stream_render.py` — add to `StreamRenderer` (near
`note_detached_spawn`):

```python
    def adopt_resumed_card(self, card: "SubAgentWidget", job_id: str) -> None:
        """Re-arm an interrupted card whose spawn was just resumed as ``job_id``:
        flip it live, route the resumed run's stream back into it, and map the
        job so the settle fills it like any detached spawn."""
        card.status = "pending"
        card._t0 = time.monotonic()
        card._t_end = None
        card.detached = True
        card.job_id = job_id
        self.tool_widgets[card.stream_id] = card
        self._detached_cards[job_id] = card
        card._paint_header()
        card._paint_activity()
```

(add `import time` if the module lacks it).

`src/marim_harness/interfaces/tui/subagents_viewer.py` — add:

```python
    def resume_selected(self) -> None:
        """The `r` key: resume the selected interrupted spawn as a background
        job. A no-op on any other status."""
        ordered = self._ordered()
        if not ordered or not (0 <= self.index < len(ordered)):
            return
        card = ordered[self.index]
        if card.status != "interrupted":
            return
        self.app.run_worker(self._resume(card))

    async def _resume(self, card) -> None:
        resume = self.app.harness.deps.services.resume_subagent
        if resume is None:
            return
        job_id, message = await resume(card.stream_id)
        if job_id is None:
            # Refused: surface the reason in the card's pane; the card stays
            # interrupted so the user can retry after fixing the cause.
            if card.pane is not None:
                card.pane.append_error(message)
        else:
            self.app.stream.adopt_resumed_card(card, job_id)
        self._repaint_list()
```

`src/marim_harness/interfaces/tui/widgets/subagents_view.py` — add to `BINDINGS`:

```python
        Binding("r", "resume_agent", "Resume", show=False),
```

and the action:

```python
    def action_resume_agent(self) -> None:
        """Resume the selected interrupted sub-agent (the 'r' key)."""
        self.app.subagents.resume_selected()
```

Also add `resume` to the `_HINTS` footer string in that file (match its existing format,
e.g. append ` · r resume`).

- [ ] **Step 7: Run the tests**

Run: `uv run pytest --no-cov tests/test_subagent_resume.py tests/test_app.py -v`
Expected: PASS.

- [ ] **Step 8: Lint, type-check, full suite, commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
git add src/marim_harness tests/test_subagent_resume.py
git commit -m "feat(subagents): resume interrupted spawns from the sub-agents screen"
```

---

### Task 6: End-to-end verification

**Files:** none created — a manual/live check plus the full gate.

- [ ] **Step 1: Full gate in CI order**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: all green.

- [ ] **Step 2: Live smoke test (free model only)**

Never a paid model without explicit approval. Run the TUI on the free live-test model:

```bash
MARIM_MODEL=openrouter/owl-alpha uv run marim
```

1. Ask for a background spawn (e.g. "spawn a background explore agent to summarize the repo"),
   let it finish, quit.
2. `MARIM_MODEL=openrouter/owl-alpha uv run marim --resume` (or the resume flag the CLI
   router exposes — check `uv run marim --help`): the jobs panel shows the settled job
   `(done)`, ctrl+x shows the spawn's card with its report, opening it lazy-loads the
   transcript.
3. Start a spawn and kill the process mid-run (Ctrl-C twice / kill). Resume: the card
   shows ⏸ interrupted; press `r` in ctrl+x; the card goes live, streams, finishes, and
   the report arrives in the next turn's finished-job digest.

- [ ] **Step 3: Commit any smoke-test fixes, then finish**

Use superpowers:finishing-a-development-branch (or report results if executing inline).
