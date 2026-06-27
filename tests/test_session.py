import json
import stat as _stat
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from marim_harness.hooks import events as hook_events
from marim_harness.hooks.runner import HookRunner
from marim_harness.runtime.deps import Deps
from marim_harness.session import SessionManager, SessionStore
from marim_harness.session.ctrl import SessionController


def _history() -> list:
    return Agent(TestModel(), instructions="x").run_sync("hi").all_messages()


def _manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")


def _write_raw(mgr: SessionManager, session_id: str, *, name=None, updated="",
               messages=None, tokens=None) -> None:
    mgr.dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": session_id,
        "name": name or session_id,
        "updated": updated,
        "tokens": tokens or {"input": 0, "output": 0},
        "messages": messages or [],
    }
    (mgr.dir / f"{session_id}.json").write_text(json.dumps(payload))


def test_create_save_and_load_roundtrip(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("My Work")
    history = _history()
    store.save(history, RunUsage(input_tokens=12, output_tokens=8))

    # A fresh store for the same id loads the saved conversation.
    again = mgr.store(store.session_id)
    messages, usage, tasks, _ = again.load()
    assert len(messages) == len(history)
    assert type(messages[0]).__name__ == type(history[0]).__name__
    assert usage.total_tokens == 20
    assert tasks == []


def test_image_bytes_survive_save_and_load(tmp_path: Path, monkeypatch):
    """A pasted image must come back byte-identical after a save/load. pydantic_ai
    serializes BinaryContent.data as URL-safe base64, so the cache round-trip must
    use the same alphabet — standard base64 silently corrupts any image whose
    payload maps to '+'/'/' and the model then rejects it as corrupt multimodal
    data on every resumed turn."""
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "imgcache"))
    # Bytes chosen so standard base64 yields '+'/'/' (URL-safe yields '-'/'_').
    raw = bytes([0xff, 0xff, 0xff, 0xfb, 0xef, 0xbe]) * 4
    mgr = _manager(tmp_path)
    store = mgr.create("Has Image")
    history = [
        ModelRequest(parts=[UserPromptPart(content=[
            "look at this", BinaryContent(data=raw, media_type="image/png"),
        ])])
    ]
    store.save(history, RunUsage())

    messages, _, _, _ = mgr.store(store.session_id).load()
    loaded = messages[0].parts[0].content[1]
    assert isinstance(loaded, BinaryContent)
    assert loaded.data == raw  # byte-identical, not double-encoded/garbled


def test_usage_round_trips_all_fields(tmp_path: Path):
    # The full RunUsage must survive a save/load, not just input+output:
    # requests, cache tokens, and details were previously dropped, so a
    # resumed session under-reported its usage.
    mgr = _manager(tmp_path)
    store = mgr.create("Rich Usage")
    usage = RunUsage(
        input_tokens=30,
        output_tokens=70,
        requests=4,
        cache_read_tokens=5,
        cache_write_tokens=3,
        input_audio_tokens=1,
        output_audio_tokens=2,
        details={"reasoning": 9},
    )
    store.save(_history(), usage)

    _, loaded, _, _ = mgr.store(store.session_id).load()
    assert loaded == usage
    assert loaded.total_tokens == usage.total_tokens
    assert loaded.requests == 4
    assert loaded.cache_read_tokens == 5
    assert loaded.cache_write_tokens == 3
    assert loaded.details == {"reasoning": 9}


def test_load_missing_returns_empty(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create()
    messages, usage, tasks, _ = store.load()
    assert messages == []
    assert usage.total_tokens == 0
    assert tasks == []


def test_load_corrupt_json_raises_clear_error(tmp_path: Path):
    """A corrupt session file (e.g. a pre-atomic-write crash) must fail loudly and
    namedly on load, not silently return an empty history that looks like the
    conversation vanished. list() still skips it (tested separately)."""
    from marim_harness.session.store import SessionLoadError

    mgr = _manager(tmp_path)
    store = mgr.create()
    store.save(_history(), RunUsage(), [])
    store.path.write_text("{ this is not valid json")
    with pytest.raises(SessionLoadError) as ei:
        store.load()
    assert str(store.path) in str(ei.value)  # the message points at the file


def test_list_skips_corrupt_while_load_raises(tmp_path: Path):
    """The asymmetry is intentional: a corrupt sibling shouldn't break the picker,
    but resuming that specific session should not silently start empty."""
    from marim_harness.session.store import SessionLoadError

    mgr = _manager(tmp_path)
    store = mgr.create()
    store.save(_history(), RunUsage(), [])
    store.path.write_text("garbage")
    assert mgr.list() == []  # picker skips it
    with pytest.raises(SessionLoadError):
        store.load()


def test_tasks_round_trip(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("With Tasks")
    tasks = [
        {"text": "first", "status": "done"},
        {"text": "second", "status": "in_progress"},
    ]
    store.save(_history(), RunUsage(), tasks)
    _, _, loaded, _ = mgr.store(store.session_id).load()
    assert loaded == tasks


def test_legacy_file_without_tasks_loads_empty(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("Legacy")
    # Save then strip the tasks key to mimic a pre-task-tracking file.
    store.save(_history(), RunUsage())
    data = json.loads(store.path.read_text())
    del data["tasks"]
    store.path.write_text(json.dumps(data))
    _, _, loaded, _ = mgr.store(store.session_id).load()
    assert loaded == []


def test_dir_is_workspace_specific(tmp_path: Path):
    base = tmp_path / "data"
    a = SessionManager(tmp_path / "ws-a", base_dir=base)
    b = SessionManager(tmp_path / "ws-b", base_dir=base)
    a_again = SessionManager(tmp_path / "ws-a", base_dir=base)
    assert a.dir != b.dir
    assert a.dir == a_again.dir  # stable per workspace


def test_create_slugifies_name(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("Fix the Parser!")
    assert store.session_id == "fix-the-parser"
    assert store.name == "Fix the Parser!"


def test_create_makes_unique_ids(tmp_path: Path):
    mgr = _manager(tmp_path)
    a = mgr.create("dup")
    b = mgr.create("dup")  # same name, even before either is saved
    assert a.session_id != b.session_id


def test_list_sorted_by_recency(tmp_path: Path):
    mgr = _manager(tmp_path)
    _write_raw(mgr, "old", updated="2026-01-01T00:00:00+00:00")
    _write_raw(mgr, "new", updated="2026-06-01T00:00:00+00:00")
    _write_raw(mgr, "mid", updated="2026-03-01T00:00:00+00:00")
    ids = [info.id for info in mgr.list()]
    assert ids == ["new", "mid", "old"]


def test_list_reports_counts_and_tokens(tmp_path: Path):
    mgr = _manager(tmp_path)
    _write_raw(
        mgr, "s1", name="Session One", updated="2026-01-01T00:00:00+00:00",
        messages=[{}, {}, {}], tokens={"input": 30, "output": 70},
    )
    info = mgr.list()[0]
    assert info.name == "Session One"
    assert info.message_count == 3
    assert info.tokens == 100


def test_save_writes_message_count_header_and_list_prefers_it(tmp_path: Path):
    """save() records a cheap message_count in the header so list() doesn't have
    to count the (possibly multi-MB) messages array; the count still matches."""
    mgr = _manager(tmp_path)
    store = mgr.create("Counted")
    history = _history()
    store.save(history, RunUsage())

    raw = json.loads((mgr.dir / f"{store.session_id}.json").read_text())
    assert raw["message_count"] == len(history)

    # list() reports the header count even when the messages array is emptied —
    # proving it reads the header field rather than re-counting the messages.
    raw["messages"] = []
    (mgr.dir / f"{store.session_id}.json").write_text(json.dumps(raw))
    info = next(i for i in mgr.list() if i.id == store.session_id)
    assert info.message_count == len(history)


def test_list_empty_when_no_sessions(tmp_path: Path):
    assert _manager(tmp_path).list() == []


def test_latest_returns_most_recent(tmp_path: Path):
    mgr = _manager(tmp_path)
    _write_raw(mgr, "old", updated="2026-01-01T00:00:00+00:00")
    _write_raw(mgr, "new", updated="2026-06-01T00:00:00+00:00")
    assert mgr.latest().id == "new"


def test_latest_none_when_empty(tmp_path: Path):
    assert _manager(tmp_path).latest() is None


def test_store_recovers_name_from_file(tmp_path: Path):
    mgr = _manager(tmp_path)
    created = mgr.create("Recover Me")
    created.save(_history(), RunUsage())
    reopened = mgr.store(created.session_id)  # no name passed
    assert reopened.name == "Recover Me"


def test_delete_removes_session(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("doomed")
    store.save(_history(), RunUsage())
    assert store.path.exists()
    mgr.delete(store.session_id)
    assert not store.path.exists()
    assert mgr.list() == []


def test_clear_removes_file(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create()
    store.save(_history(), RunUsage())
    assert store.path.exists()
    store.clear()
    assert not store.path.exists()
    store.clear()  # idempotent


def test_store_is_a_session_store(tmp_path: Path):
    mgr = _manager(tmp_path)
    assert isinstance(mgr.create(), SessionStore)


def test_unnamed_session_is_auto_named(tmp_path: Path):
    assert _manager(tmp_path).create().auto_named is True


def test_named_session_is_not_auto_named(tmp_path: Path):
    assert _manager(tmp_path).create("My Work").auto_named is False


def test_auto_flag_persists_and_recovers(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create()  # auto_named True
    store.save(_history(), RunUsage())
    assert mgr.store(store.session_id).auto_named is True

    # Once titled, the flag is off and stays off across reopens.
    store.name = "Picked a title"
    store.auto_named = False
    store.save(_history(), RunUsage())
    assert mgr.store(store.session_id).auto_named is False
    assert mgr.store(store.session_id).name == "Picked a title"


def test_model_persists_and_recovers(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create()
    assert store.model is None  # defaults to the env model
    store.model = "openai/gpt-5.2"
    store.save(_history(), RunUsage())
    assert mgr.store(store.session_id).model == "openai/gpt-5.2"


def test_latest_model_returns_most_recent(tmp_path: Path):
    mgr = _manager(tmp_path)
    _write_raw(
        mgr, "old", updated="2026-01-01T00:00:00+00:00",
        messages=[{}, {}],
    )
    # Write a session with a model set.
    mgr.dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": "new",
        "name": "new",
        "updated": "2026-06-01T00:00:00+00:00",
        "model": "openai/gpt-5.2",
        "tokens": {"input": 0, "output": 0},
        "messages": [],
    }
    (mgr.dir / "new.json").write_text(json.dumps(payload))
    assert mgr.latest_model() == "openai/gpt-5.2"


def test_latest_model_none_when_no_sessions(tmp_path: Path):
    assert _manager(tmp_path).latest_model() is None


def test_latest_model_none_when_no_model_set(tmp_path: Path):
    mgr = _manager(tmp_path)
    _write_raw(mgr, "s1", updated="2026-06-01T00:00:00+00:00")
    assert mgr.latest_model() is None


def test_create_inherits_model_from_latest_session(tmp_path: Path):
    mgr = _manager(tmp_path)
    # Create and save a session with a model.
    first = mgr.create("First")
    first.model = "openai/gpt-5.2"
    first.save(_history(), RunUsage())
    # A brand-new session should inherit that model.
    second = mgr.create("Second")
    assert second.model == "openai/gpt-5.2"


def test_create_no_model_when_no_sessions(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("Alone")
    assert store.model is None


# ---------------------------------------------------------------------------
# PreCompact hook tests
# ---------------------------------------------------------------------------


def _hook_cmd(tmp_path, log):
    p = tmp_path / "pc.sh"
    p.write_text(f"#!/usr/bin/env bash\ncat >> {log}\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | _stat.S_IEXEC | _stat.S_IRWXU)
    return str(p)


@pytest.mark.anyio
async def test_pre_compact_fires_when_compaction_runs(tmp_path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    log = tmp_path / "pc.log"
    cmd = _hook_cmd(tmp_path, log)
    deps = Deps(
        workspace_root=tmp_path,
        hooks=HookRunner(
            {hook_events.PRE_COMPACT: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    # A tiny token budget forces compaction of a non-trivial history.
    ctrl = SessionController(None, None, deps, max_context_tokens=1, keep_last_messages=1)
    ctrl.history = [
        ModelRequest(parts=[UserPromptPart(content="x" * 5000)]),
        ModelRequest(parts=[UserPromptPart(content="y" * 5000)]),
        ModelRequest(parts=[UserPromptPart(content="z" * 5000)]),
    ]
    await ctrl.maybe_compact()
    assert log.exists()
    assert '"hook_event_name": "PreCompact"' in log.read_text()


@pytest.mark.anyio
async def test_pre_compact_fires_before_compaction_work(tmp_path):
    """PreCompact must run BEFORE the compaction work, not after it. The whole
    point of the hook (matching Claude Code) is to let a tool snapshot the full
    transcript before it's summarized/collapsed — so the dispatch has to precede
    the (potentially expensive) summarizer call, not trail it."""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    order: list[str] = []

    class _RecordingHooks:
        async def dispatch(self, event, payload):
            order.append(f"hook:{event}")

    async def _summarizer(middle):
        order.append("summarizer")
        return "SUMMARY"

    deps = Deps(workspace_root=tmp_path, hooks=_RecordingHooks())
    ctrl = SessionController(
        None, None, deps, max_context_tokens=1, keep_last_messages=1,
        summarizer=_summarizer,
    )
    ctrl.history = [
        ModelRequest(parts=[UserPromptPart(content="x" * 5000)]),
        ModelRequest(parts=[UserPromptPart(content="y" * 5000)]),
        ModelRequest(parts=[UserPromptPart(content="z" * 5000)]),
    ]
    await ctrl.maybe_compact()
    assert order == [f"hook:{hook_events.PRE_COMPACT}", "summarizer"]


@pytest.mark.anyio
async def test_pre_compact_does_not_fire_without_compaction(tmp_path):
    log = tmp_path / "pc.log"
    cmd = _hook_cmd(tmp_path, log)
    deps = Deps(
        workspace_root=tmp_path,
        hooks=HookRunner(
            {hook_events.PRE_COMPACT: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    ctrl = SessionController(None, None, deps, max_context_tokens=100_000, keep_last_messages=20)
    ctrl.history = []  # nothing to compact
    await ctrl.maybe_compact()
    assert not log.exists()


@pytest.mark.anyio
async def test_on_compact_start_fires_before_finish_when_compacting(tmp_path):
    deps = Deps(workspace_root=tmp_path)
    ctrl = SessionController(None, None, deps, max_context_tokens=1, keep_last_messages=1)
    ctrl.history = [
        ModelRequest(parts=[UserPromptPart(content="x" * 5000)]),
        ModelRequest(parts=[UserPromptPart(content="y" * 5000)]),
        ModelRequest(parts=[UserPromptPart(content="z" * 5000)]),
    ]
    events: list[str] = []
    ctrl.on_compact_start = lambda: events.append("start")
    ctrl.on_compact = lambda before, after: events.append("done")
    await ctrl.maybe_compact()
    assert events == ["start", "done"]


@pytest.mark.anyio
async def test_on_compact_start_not_fired_without_compaction(tmp_path):
    deps = Deps(workspace_root=tmp_path)
    ctrl = SessionController(None, None, deps, max_context_tokens=100_000, keep_last_messages=20)
    ctrl.history = []  # nothing to compact
    fired: list[int] = []
    ctrl.on_compact_start = lambda: fired.append(1)
    await ctrl.maybe_compact()
    assert fired == []


@pytest.mark.anyio
async def test_forced_compaction_clears_indicator_even_without_shrink(tmp_path):
    # A forced compaction (used after a provider context-overflow where the token
    # estimate undershot) can run yet not shrink history. The "compacting…"
    # indicator must still be cleared: on_compact fires with before == after so
    # the UI just drops the notice instead of leaving a stuck spinner.
    deps = Deps(workspace_root=tmp_path)
    ctrl = SessionController(
        None, None, deps, max_context_tokens=100_000, keep_last_messages=20
    )
    ctrl.history = [ModelRequest(parts=[UserPromptPart(content="small")])]
    events: list = []
    ctrl.on_compact_start = lambda: events.append("start")
    ctrl.on_compact = lambda before, after: events.append(("done", before, after))
    did = await ctrl.maybe_compact(force=True)
    assert did is False  # nothing to shrink
    assert "start" in events  # the indicator was shown
    done = [e for e in events if isinstance(e, tuple) and e[0] == "done"]
    assert done, "on_compact must fire to clear the indicator even with no shrink"
    assert done[0][1] == done[0][2]  # before == after: the clear-only signal


@pytest.mark.anyio
async def test_compaction_persists_the_compacted_history(tmp_path):
    """A compaction that fires must be persisted, so a process death between turns
    doesn't lose it (and the on-disk file matches the in-memory history)."""
    mgr = _manager(tmp_path)
    store = mgr.create("compact me")
    deps = Deps(workspace_root=tmp_path)
    ctrl = SessionController(
        store, mgr, deps, max_context_tokens=1, keep_last_messages=1
    )
    ctrl.history = [
        ModelRequest(parts=[UserPromptPart(content="x" * 5000)]),
        ModelRequest(parts=[UserPromptPart(content="y" * 5000)]),
        ModelRequest(parts=[UserPromptPart(content="z" * 5000)]),
    ]
    await ctrl.maybe_compact()
    compacted_len = len(ctrl.history)
    assert compacted_len < 3  # the compaction actually fired
    # A fresh load from disk must reflect the compacted history, not the full one.
    reloaded, _, _, _ = mgr.store(store.session_id).load()
    assert len(reloaded) == compacted_len


@pytest.mark.anyio
async def test_no_compaction_does_not_force_a_write(tmp_path):
    """When nothing compacts, maybe_compact must not bump persistence work."""
    mgr = _manager(tmp_path)
    store = mgr.create("untouched")
    deps = Deps(workspace_root=tmp_path)
    ctrl = SessionController(
        store, mgr, deps, max_context_tokens=100_000, keep_last_messages=20
    )
    ctrl.history = [ModelRequest(parts=[UserPromptPart(content="small")])]
    ctrl.persist()  # establish the on-disk baseline
    before = store.path.read_text()
    await ctrl.maybe_compact()
    assert store.path.read_text() == before  # nothing rewritten


# ---------------------------------------------------------------------------
# saved_model_id and update_model encapsulation tests
# ---------------------------------------------------------------------------


def test_saved_model_id_returns_store_model(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("with-model")
    store.model = "anthropic/claude-3-7"
    deps = Deps(workspace_root=tmp_path)
    ctrl = SessionController(store, mgr, deps, 100_000, 20)
    assert ctrl.saved_model_id == "anthropic/claude-3-7"


def test_saved_model_id_none_without_store(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path)
    ctrl = SessionController(None, None, deps, 100_000, 20)
    assert ctrl.saved_model_id is None


@pytest.mark.anyio
async def test_update_model_rebuilds_summarizer_and_titler(tmp_path: Path):
    """update_model swaps both aux agents when originally configured."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    model_b = FunctionModel(fn)

    async def _stub_summarizer(mid):
        return "summary"

    async def _stub_titler(history):
        return "title"

    deps = Deps(workspace_root=tmp_path)
    ctrl = SessionController(
        None, None, deps, 100_000, 20,
        summarizer=_stub_summarizer,
        titler=_stub_titler,
    )
    original_summarizer = ctrl.summarizer
    original_titler = ctrl.titler
    ctrl.update_model(model_b)
    # The aux agents were replaced (new callable objects).
    assert ctrl.summarizer is not original_summarizer
    assert ctrl.titler is not original_titler


def test_update_model_leaves_none_aux_agents_as_none(tmp_path: Path):
    """update_model must not install aux agents that weren't originally configured."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = Deps(workspace_root=tmp_path)
    ctrl = SessionController(None, None, deps, 100_000, 20)  # no summarizer/titler
    ctrl.update_model(FunctionModel(fn))
    assert ctrl.summarizer is None
    assert ctrl.titler is None


def test_session_save_load_round_trips_image(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "imgs"))
    mgr = SessionManager(tmp_path / "ws", base_dir=tmp_path / "sessions")
    store = mgr.create("with-image")
    history = [ModelRequest(parts=[UserPromptPart(
        content=["see this", BinaryContent(data=b"\x89PNGz", media_type="image/png")]
    )])]
    store.save(history, RunUsage())
    # session JSON must not carry the base64 payload inline
    assert "marim-image-cache://" in store.path.read_text()
    loaded, _usage, _tasks, _ = store.load()
    parts = loaded[0].parts
    binaries = [c for c in parts[0].content if isinstance(c, BinaryContent)]
    assert binaries and binaries[0].data == b"\x89PNGz"
