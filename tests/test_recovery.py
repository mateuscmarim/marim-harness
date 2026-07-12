"""Regression tests for resuming a session left mid-exchange by an aborted turn.

A turn that dies after the model emits a tool call but before the tool returns
(API outage, usage limit, cancel) can persist a ``ToolCallPart`` with no
matching ``ToolReturnPart``. Every provider then rejects the next request with
"unprocessed tool calls", wedging the session. These cover the repair helper
and the loop paths that must heal — or never persist — such a history.
"""

import json

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from marim_harness.runtime.harness import (
    Harness,
    _drop_nameless_tool_calls,
    _has_unanswered_tool_calls,
    _repair_unanswered_tool_calls,
)
from marim_harness.runtime.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps

pytestmark = pytest.mark.anyio


def _harness(model, deps):
    return Harness(
        model=model,
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions="You are a coding agent.",
    )


def _dangling_history():
    """A history ending in an unanswered tool call — the wedged shape."""
    return [
        ModelRequest(parts=[UserPromptPart(content="do something")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="edit_file", args={}, tool_call_id="tc-stuck")]
        ),
    ]


# --- unit: the repair helper -------------------------------------------------


def test_repair_is_noop_on_clean_history():
    clean = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(parts=[TextPart(content="hello")]),
    ]
    # Same object back, so callers can skip a redundant persist.
    assert _repair_unanswered_tool_calls(clean) is clean


def test_repair_synthesizes_return_for_dangling_call():
    history = _dangling_history()
    assert _has_unanswered_tool_calls(history)
    repaired = _repair_unanswered_tool_calls(history)
    assert not _has_unanswered_tool_calls(repaired)
    returns = [
        p
        for m in repaired
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolReturnPart)
    ]
    assert [r.tool_call_id for r in returns] == ["tc-stuck"]
    assert returns[0].tool_name == "edit_file"


def test_repair_localizes_each_return_after_its_response():
    """A dangling call mid-history gets its return inserted right after the
    response that made it (provider requirement), not lumped at the very end."""
    history = [
        ModelResponse(
            parts=[ToolCallPart(tool_name="read_file", args={}, tool_call_id="a")]
        ),
        ModelResponse(
            parts=[ToolCallPart(tool_name="grep", args={}, tool_call_id="b")]
        ),
    ]
    repaired = _repair_unanswered_tool_calls(history)
    shape = [
        (type(m).__name__, [getattr(p, "tool_call_id", None) for p in m.parts])
        for m in repaired
    ]
    assert shape == [
        ("ModelResponse", ["a"]),
        ("ModelRequest", ["a"]),
        ("ModelResponse", ["b"]),
        ("ModelRequest", ["b"]),
    ]
    assert not _has_unanswered_tool_calls(repaired)


# --- unit: dropping nameless (malformed) tool calls --------------------------
#
# A model/provider can stream a partial tool call whose function name never
# arrives, leaving a ToolCallPart with an empty tool_name. Persisted, every
# provider then rejects the next request ("tool_calls[i] is missing a function
# name"), wedging the session exactly like a dangling call does — but the
# unanswered-call repair can't see it (the part HAS an id, it's just nameless).


def test_drop_nameless_is_noop_on_clean_history():
    clean = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="read_file", args={}, tool_call_id="a")]
        ),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="read_file", content="x", tool_call_id="a"
        )]),
    ]
    # Same object back, so callers can skip a redundant persist.
    assert _drop_nameless_tool_calls(clean) is clean


def test_drop_nameless_removes_the_nameless_call_keeps_the_valid_one():
    history = [
        ModelRequest(parts=[UserPromptPart(content="go")]),
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="read_file", args={}, tool_call_id="ok"),
                ToolCallPart(tool_name="", args={}, tool_call_id="bad"),
            ]
        ),
    ]
    cleaned = _drop_nameless_tool_calls(history)
    calls = [
        p
        for m in cleaned
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolCallPart)
    ]
    assert [c.tool_call_id for c in calls] == ["ok"]


def test_drop_nameless_also_drops_a_return_orphaned_by_the_removal():
    """A ToolReturnPart answering a nameless call must go too — once its call is
    gone the return references nothing and is itself rejected."""
    history = [
        ModelResponse(
            parts=[ToolCallPart(tool_name="", args={}, tool_call_id="bad")]
        ),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="", content="x", tool_call_id="bad"
        )]),
    ]
    cleaned = _drop_nameless_tool_calls(history)
    remaining_ids = [
        getattr(p, "tool_call_id", None)
        for m in cleaned
        for p in getattr(m, "parts", [])
    ]
    assert "bad" not in remaining_ids


def test_drop_nameless_drops_a_message_emptied_by_the_removal():
    """A ModelResponse whose only part was the nameless call has nothing left to
    say; the empty message is dropped rather than sent as a contentless turn."""
    history = [
        ModelRequest(parts=[UserPromptPart(content="go")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="", args={}, tool_call_id="bad")]
        ),
    ]
    cleaned = _drop_nameless_tool_calls(history)
    assert len(cleaned) == 1
    assert isinstance(cleaned[0], ModelRequest)


def test_drop_nameless_keeps_other_parts_of_a_mixed_message():
    """The model can emit reasoning text alongside the malformed call; only the
    nameless part is stripped, the text survives."""
    history = [
        ModelResponse(
            parts=[
                TextPart(content="let me read that"),
                ToolCallPart(tool_name="", args={}, tool_call_id="bad"),
            ]
        ),
    ]
    cleaned = _drop_nameless_tool_calls(history)
    parts = list(cleaned[0].parts)
    assert len(parts) == 1
    assert isinstance(parts[0], TextPart)


# A model can also emit a *named* tool call whose arguments aren't valid JSON
# (a truncated stream, a provider that forwards raw text). Persisted, the next
# request 400s with "Assistant tool call function.arguments must be valid JSON".
# The nameless check misses it — the part HAS a name — so the scrub must also
# drop a call whose args string won't parse.


def test_drop_unusable_removes_a_call_with_malformed_json_args():
    history = [
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="bash", args='{"command": "ls"', tool_call_id="ok"),
            ]
        ),
    ]
    cleaned = _drop_nameless_tool_calls(history)
    calls = [
        p
        for m in cleaned
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolCallPart)
    ]
    assert calls == []  # the truncated-JSON call is structurally unusable → gone


def test_drop_unusable_keeps_a_call_with_valid_json_string_args():
    """A guard against over-removal: args given as a *valid* JSON string is fine —
    providers accept it — so the scrub must leave it (and stay a noop)."""
    history = [
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="bash", args='{"command": "ls"}', tool_call_id="ok"),
            ]
        ),
    ]
    assert _drop_nameless_tool_calls(history) is history


async def test_resume_strips_nameless_tool_call_then_runs(tmp_path):
    """End-to-end: a persisted history carrying a nameless tool call must resume
    on the next prompt — the malformed call is stripped before the request, so
    the provider never sees the 'missing a function name' 400."""
    deps = _make_deps(tmp_path)

    def reply(messages, info):
        return ModelResponse(parts=[TextPart(content="resumed")])

    harness = _harness(FunctionModel(reply), deps)
    harness.session.history = [
        ModelRequest(parts=[UserPromptPart(content="go")]),
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="read_file", args={}, tool_call_id="ok"),
                ToolCallPart(tool_name="", args={}, tool_call_id="bad"),
            ]
        ),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="read_file", content="data", tool_call_id="ok"
        )]),
    ]

    output = await harness.run_turn("continue")  # must NOT raise

    assert output == "resumed"
    calls = [
        p
        for m in harness.session.history
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolCallPart)
    ]
    assert all(c.tool_name for c in calls)  # no nameless call survived


async def test_nameless_call_stripped_before_every_request_not_just_resume(tmp_path):
    """A nameless tool call the model emits LIVE mid-turn (not from persisted
    history) must be stripped before the *next* model request. The turn-start
    sanitizer can't see it — it runs once, before the turn — so this relies on a
    ProcessHistory capability that runs before every request. Reproduces the
    reported timeline: after a rewind cleared the old garbage, a fresh 'continue'
    proceeded and then the flaky model emitted another nameless call that 400'd."""
    deps = _make_deps(tmp_path)
    calls = {"n": 0}
    seen: dict = {}

    def fn(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            # A flaky provider can stream a tool call whose name never arrives.
            return ModelResponse(
                parts=[ToolCallPart(tool_name="", args={}, tool_call_id="bad")]
            )
        seen["messages"] = messages
        return ModelResponse(parts=[TextPart(content="done")])

    harness = _harness(FunctionModel(fn), deps)
    output = await harness.run_turn("go")

    assert output == "done"
    # The continuation request the model saw must not carry the nameless call.
    nameless = [
        p
        for m in seen["messages"]
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolCallPart) and not p.tool_name
    ]
    assert nameless == []


# --- e2e: self-heal on resume (the reported bug) -----------------------------


async def test_resume_heals_dangling_tool_call_then_runs(tmp_path):
    """The reported symptom: a session whose persisted history ends in an
    unanswered tool call must resume on the next prompt, not raise pydantic-ai's
    'Cannot provide a new user prompt ... unprocessed tool calls' UserError."""
    deps = _make_deps(tmp_path)

    def reply(messages, info):
        return ModelResponse(parts=[TextPart(content="resumed")])

    harness = _harness(FunctionModel(reply), deps)
    harness.session.history = _dangling_history()

    output = await harness.run_turn("continue")  # must NOT raise

    assert output == "resumed"
    assert not _has_unanswered_tool_calls(harness.session.history)


# --- the turn-start checkpoint records the SANITIZED history length -----------


async def test_checkpoint_records_sanitized_length_for_clean_rewind(tmp_path):
    """A Checkpoint's ``history_len`` is an absolute index, and the turn-start
    sanitize/repair changes history length (the repair inserts a synthesized
    return for the dangling call). The checkpoint must be captured AFTER that
    sanitize, so a later /rewind slices on a clean boundary — not mid-pair,
    stranding the tool call from the very return the repair just added, which is
    the unresumable shape this subsystem exists to prevent.

    Regression: the snapshot used to run before the sanitize, recording the
    pre-repair length (2 here), so rewinding sliced ``history[:2]`` right back to
    the dangling call.
    """
    deps = _make_deps(tmp_path)

    def reply(messages, info):
        return ModelResponse(parts=[TextPart(content="resumed")])

    harness = _harness(FunctionModel(reply), deps)
    # Incoming history ends in an unanswered tool call (2 messages, the wedged shape).
    harness.session.history = _dangling_history()

    out = await harness.run_turn("continue")
    assert out == "resumed"

    cps = harness.checkpoints.list()
    assert len(cps) == 1
    # The repair inserted a synthesized return before the turn ran, so the
    # sanitized baseline is 3 messages — the checkpoint must have recorded that,
    # not the pre-sanitize 2 (which would slice off the tool call's return).
    assert cps[0].history_len == 3

    harness.checkpoints.rewind(cps[0].index)
    # Rewinding lands on a resumable boundary, never back on a dangling tool call.
    assert not _has_unanswered_tool_calls(harness.session.history)
    assert len(harness.session.history) == 3


# --- an orphaned resumable-flush persist can't clobber a newer write ----------


async def test_orphaned_flush_persist_does_not_clobber_newer_write(tmp_path):
    """``_flush_resumable`` runs ``persist`` under a 0.25s deadline and *abandons*
    the worker thread on timeout without stopping it. If that orphan later
    completes, it must NOT overwrite a newer on-disk session with its stale
    recovered history. ``SessionController.persist`` serializes writers under a
    lock and re-checks a monotonic ``history_version`` inside it, so the abandoned
    orphan either finishes before the newer write (which then overwrites it) or
    no-ops. This pins that guarantee for the exact orphan-then-newer ordering the
    flush can produce.
    """
    import threading

    from marim_harness.session.ctrl import SessionController

    deps = _make_deps(tmp_path)

    class _BlockingStore:
        """Records each save's history snapshot in write order; the FIRST save
        blocks (simulating the stalled disk that made the flush orphan its
        thread) until the test releases it."""

        session_id = "s"
        model = None

        def __init__(self) -> None:
            self.saved: list[list] = []
            self.entered = threading.Event()
            self.release = threading.Event()
            self._blocked = False

        def save(self, history, usage, tasks, *, duration_seconds, jobs) -> None:
            snapshot = list(history)
            if not self._blocked:
                self._blocked = True
                self.entered.set()
                self.release.wait(2.0)
            self.saved.append(snapshot)

    store = _BlockingStore()
    ctrl = SessionController(store, None, deps, 100_000, 20)

    stale = [ModelRequest(parts=[UserPromptPart(content="STALE recovered")])]
    newer = [ModelRequest(parts=[UserPromptPart(content="NEWER turn")])]

    # The orphaned flush: sets the recovered history and persists on a thread that
    # blocks inside store.save, holding the persist lock.
    ctrl.history = stale
    orphan = threading.Thread(target=ctrl.persist)
    orphan.start()
    assert store.entered.wait(2.0)  # orphan is now "writing" the stale history

    # A newer turn lands a newer history and its own persist while the orphan is
    # still stuck. Its write blocks on the lock the orphan holds.
    ctrl.history = newer
    newer_writer = threading.Thread(target=ctrl.persist)
    newer_writer.start()

    store.release.set()  # let the orphan finish, then the newer writer proceeds
    orphan.join(2.0)
    newer_writer.join(2.0)
    assert not orphan.is_alive() and not newer_writer.is_alive()

    # Both writes happened (orphan first), but the NEWER history landed LAST — the
    # abandoned orphan never clobbered it.
    assert store.saved == [stale, newer]
    assert store.saved[-1] == newer


# --- first turn writes a resumable baseline before the model runs ------------


async def test_first_turn_persists_a_baseline_before_the_model_runs(tmp_path):
    """A brand-new session has no file on disk until its first *successful*
    end-of-turn persist. A long first turn (a deep-research fan-out) that is
    hard-killed mid-run would then leave ZERO trace: no session to resume and its
    sub-agent sidecars orphaned — the real lost-session bug. The turn must write a
    clean baseline BEFORE the model runs, so an interrupt at any later point still
    leaves a resumable session."""
    from marim_harness.session import SessionStore
    from tests.conftest import _make_harness

    store = SessionStore(path=tmp_path / "sessions" / "s.json",
                         workspace_root=tmp_path, session_id="s", name="s")
    existed_at_model_time: dict[str, bool] = {}

    def fn(messages, info):
        existed_at_model_time["v"] = store.path.exists()
        return ModelResponse(parts=[TextPart(content="ok")])

    harness = _make_harness(FunctionModel(fn), _make_deps(tmp_path), store=store)
    assert not store.path.exists()  # nothing on disk before the turn
    await harness.run_turn("first prompt")

    assert existed_at_model_time["v"] is True  # baseline written before the model


async def test_run_turn_starts_the_active_time_clock(tmp_path):
    """A turn is active time by definition, so run_turn must ensure the segment
    clock is running even when no mount / new_session / switch started it. Before,
    a turn that ran (and was aborted) with ``_segment_start == 0`` flushed
    ``duration_seconds == 0`` despite real wall-clock work — the aborted-turn
    zero-duration bug. run_turn starts the clock, so the end-of-turn persist
    records the turn's active time."""
    import asyncio as _asyncio

    from marim_harness.session import SessionStore
    from tests.conftest import _make_harness

    store = SessionStore(path=tmp_path / "sessions" / "s.json",
                         workspace_root=tmp_path, session_id="s", name="s")

    async def fn(messages, info):
        await _asyncio.sleep(0.02)  # measurable active time
        return ModelResponse(parts=[TextPart(content="ok")])

    harness = _make_harness(FunctionModel(fn), _make_deps(tmp_path), store=store)
    harness.session._segment_start = 0.0  # no mount/new/switch ran

    await harness.run_turn("hi")

    assert harness.session._segment_start != 0.0  # run_turn started the clock
    _, _, _, dur, _ = store.load()
    assert dur is not None and dur > 0  # the turn's active time was recorded


# --- a failed, output-less turn rolls back its own checkpoint ----------------


async def test_failed_turn_with_no_model_output_drops_its_checkpoint(tmp_path):
    """A turn that errors before producing any model response is a dead rewind
    target — its checkpoint preview is just the failed prompt and rewinding to it
    lands right before a turn that did nothing. The start-of-turn checkpoint is
    rolled back (the bare prompt still persists for resumability; only the useless
    checkpoint goes), so /rewind lands on real assistant states."""
    deps = _make_deps(tmp_path)

    def fn(messages, info):
        raise RuntimeError("model boom")  # fails before any response

    harness = _harness(FunctionModel(fn), deps)
    before = len(harness.checkpoints.list())

    with pytest.raises(BaseException):  # noqa: B017 - model error propagates
        await harness.run_turn("continue")

    assert len(harness.checkpoints.list()) == before  # checkpoint rolled back


async def test_failed_turn_after_a_model_response_keeps_its_checkpoint(tmp_path):
    """A turn that DID produce model output (a tool call) before failing keeps its
    checkpoint — there is real work to rewind to."""
    (tmp_path / "a.txt").write_text("hello")
    deps = _make_deps(tmp_path)
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="read_file", args={"path": "a.txt"}, tool_call_id="t1"
            )])
        raise RuntimeError("boom on continuation")

    harness = _harness(FunctionModel(fn), deps)
    before = len(harness.checkpoints.list())

    with pytest.raises(BaseException):  # noqa: B017
        await harness.run_turn("read it")

    assert len(harness.checkpoints.list()) == before + 1  # checkpoint kept


# --- e2e: compaction during a turn invalidates stale checkpoints -------------


async def test_compaction_during_turn_invalidates_checkpoints(tmp_path):
    """A compaction restructures history, so the checkpoints captured against the
    pre-compaction (absolute) indices are stale and must be dropped — otherwise a
    later rewind slices at the wrong boundary and corrupts the conversation."""
    from marim_harness.runtime.harness import HarnessConfig

    deps = _make_deps(tmp_path)

    def reply(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    # Tiny budget so the first start-of-turn compaction fires on the seeded
    # history; keep only the last 2 messages.
    harness = Harness(
        model=FunctionModel(reply),
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions="You are a coding agent.",
        config=HarnessConfig(max_context_tokens=10, keep_last_messages=2),
    )
    # Seed a long, well-formed history and a checkpoint that points into it.
    harness.session.history = [
        ModelRequest(parts=[UserPromptPart(content=f"m {i}")]) for i in range(20)
    ]
    harness.checkpoints.snapshot("stale turn")
    assert harness.checkpoints.list()  # checkpoint exists pre-compaction

    await harness.run_turn("next")

    # The start-of-turn compaction shrank history, so the stale checkpoint was
    # dropped rather than left pointing at a vanished boundary.
    assert harness.session.history  # compaction kept the tail (didn't wipe)
    assert len(harness.session.history) < 20
    stale = [c for c in harness.checkpoints.list() if c.prompt_preview == "stale turn"]
    assert stale == []


# --- Part A: a failed continuation after an approval round stays resumable ----


def _deferred_then_inflight_model():
    """Turn 1 emits an edit (needs approval → deferred round); the continuation
    emits a fresh read_file call that the test aborts while still in flight."""
    n = {"stream": 0}

    def fn(messages, info):
        # Non-streaming path is unused once event_stream_handler is set, but the
        # model must still be constructable with it.
        return ModelResponse(parts=[TextPart(content="unused")])

    async def stream_fn(messages, info):
        n["stream"] += 1
        if n["stream"] == 1:
            yield {
                0: DeltaToolCall(
                    name="edit_file",
                    json_args=json.dumps(
                        {
                            "path": "a.txt",
                            "edits": [{"old_string": "foo", "new_string": "bar"}],
                        }
                    ),
                    tool_call_id="tc-edit",
                )
            }
        else:
            yield {
                0: DeltaToolCall(
                    name="read_file",
                    json_args=json.dumps({"path": "a.txt"}),
                    tool_call_id="tc-read",
                )
            }

    return FunctionModel(fn, stream_function=stream_fn), n


async def test_failed_continuation_after_approval_persists_resumable(tmp_path):
    """After an approval round, a continuation that dies with a tool call still
    in flight must persist a resumable history — never the raw dangling state.
    Reproduces the exact path that wedged the user's session (UsageLimitExceeded
    deep in a deferred continuation, then UserError on the next prompt)."""
    (tmp_path / "a.txt").write_text("foo")

    model, n = _deferred_then_inflight_model()

    async def approve(_call):
        return True

    deps = _make_deps(tmp_path, mode=Mode.ask, request_approval=approve)
    harness = _harness(model, deps)

    aborted = {"done": False}

    async def boom_handler(stream_ctx, events):
        async for _event in events:
            # Abort once we're in the continuation (after the approved edit).
            if n["stream"] >= 2:
                aborted["done"] = True
                raise RuntimeError("continuation boom")

    with pytest.raises(RuntimeError):
        await harness.run_turn(
            "change foo to bar", event_stream_handler=boom_handler
        )

    assert aborted["done"], "test did not reach the continuation"
    # The dangling read_file (and any leftover edit) must be repaired, not
    # persisted raw — otherwise the next prompt raises the UserError.
    assert not _has_unanswered_tool_calls(harness.session.history)


async def test_rollback_persist_failure_does_not_mask_cancel(tmp_path):
    """Ctrl-C during the approval wait cancels the turn; the rollback persist
    that follows is best-effort. If it hits a disk error, the ORIGINAL
    CancelledError must still propagate — an OSError surfacing instead would
    swallow the cancel and make shutdown look like a crash."""
    import asyncio

    (tmp_path / "a.txt").write_text("foo")

    def fn(messages, info):
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart" and \
                        getattr(p, "tool_name", "") == "edit_file":
                    return ModelResponse(parts=[TextPart(content="done")])
        return ModelResponse(parts=[ToolCallPart(
            tool_name="edit_file",
            args={"path": "a.txt",
                  "edits": [{"old_string": "foo", "new_string": "bar"}]},
        )])

    async def cancel_on_ask(_call):
        raise asyncio.CancelledError

    deps = _make_deps(tmp_path, mode=Mode.ask, request_approval=cancel_on_ask)
    harness = _harness(FunctionModel(fn), deps)

    def broken_persist(*args, **kwargs):
        raise OSError("disk full")

    harness.session.persist = broken_persist

    with pytest.raises(asyncio.CancelledError):
        await harness.run_turn("change foo to bar")
