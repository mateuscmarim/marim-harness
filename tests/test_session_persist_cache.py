"""Persist should skip the encode/decode round-trip and the disk write when
history hasn't changed between calls — saves several MB per turn on long
sessions."""
import threading

from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.usage import RunUsage

from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionController, SessionManager
from tests.conftest import _make_deps


def test_persist_skips_save_when_history_unchanged(tmp_path):
    """After the first persist, calling persist() again with no history
    mutation must not call store.save — the disk is already current."""
    workspace = tmp_path
    manager = SessionManager(workspace)
    store = manager.create("s1")
    deps = _make_deps(workspace, mode=Mode.ask)

    save_calls = []
    original_save = store.save

    def tracking_save(*args, **kwargs):
        save_calls.append((args, kwargs))
        return original_save(*args, **kwargs)

    store.save = tracking_save

    ctrl = SessionController(store, manager, deps, 100_000, 20)

    # First persist — actually writes.
    ctrl.set_history([{"role": "user", "content": "hi"}])
    ctrl.usage = RunUsage(input_tokens=5, output_tokens=3)
    ctrl.persist()
    assert len(save_calls) == 1

    # Second persist with NO history mutation — must NOT call save.
    ctrl.persist()
    assert len(save_calls) == 1, f"no-op persist called save: {save_calls!r}"

    # Third persist with usage-only change (no history bump) — must still skip.
    ctrl.usage = RunUsage(input_tokens=10, output_tokens=5)
    ctrl.persist()
    assert len(save_calls) == 1


def test_persist_runs_when_history_changes(tmp_path):
    """When history is mutated (via set_history), the next persist must
    actually write — the cache must be invalidated."""
    workspace = tmp_path
    manager = SessionManager(workspace)
    store = manager.create("s2")
    deps = _make_deps(workspace, mode=Mode.ask)

    save_calls = []
    original_save = store.save

    def tracking_save(*args, **kwargs):
        save_calls.append(1)
        return original_save(*args, **kwargs)

    store.save = tracking_save

    ctrl = SessionController(store, manager, deps, 100_000, 20)
    ctrl.set_history([{"role": "user", "content": "first"}])
    ctrl.persist()
    assert len(save_calls) == 1

    ctrl.set_history([{"role": "user", "content": "second"}])
    ctrl.persist()
    assert len(save_calls) == 2, "history change should invalidate cache"


def test_assign_directly_to_history_invalidates_cache(tmp_path):
    """The cache must invalidate on direct ``self.history = ...`` assignment,
    too — sites that haven't migrated to set_history must still see writes."""
    workspace = tmp_path
    manager = SessionManager(workspace)
    store = manager.create("s3")
    deps = _make_deps(workspace, mode=Mode.ask)

    save_calls = []
    original_save = store.save

    def tracking_save(*args, **kwargs):
        save_calls.append(1)
        return original_save(*args, **kwargs)

    store.save = tracking_save

    ctrl = SessionController(store, manager, deps, 100_000, 20)
    ctrl.history = [{"role": "user", "content": "a"}]
    ctrl.persist()
    assert len(save_calls) == 1

    ctrl.history = [{"role": "user", "content": "b"}]  # direct assignment
    ctrl.persist()
    assert len(save_calls) == 2, "direct assignment should invalidate cache"


def test_in_place_history_mutation_invalidates_cache(tmp_path):
    """In-place mutations (``history.append``/``+=``/``history[i] = ...``) must
    bump the version too, so the persist cache can't silently drop them — the
    list returned by ``.history`` is a version-tracking proxy."""
    workspace = tmp_path
    manager = SessionManager(workspace)
    store = manager.create("s4")
    deps = _make_deps(workspace, mode=Mode.ask)

    save_calls = []
    original_save = store.save

    def tracking_save(*args, **kwargs):
        save_calls.append(1)
        return original_save(*args, **kwargs)

    store.save = tracking_save

    ctrl = SessionController(store, manager, deps, 100_000, 20)
    ctrl.set_history([{"role": "user", "content": "a"}])
    ctrl.persist()
    assert len(save_calls) == 1

    ctrl.history.append({"role": "user", "content": "b"})
    ctrl.persist()
    assert len(save_calls) == 2, "append should invalidate cache"

    ctrl.history += [{"role": "user", "content": "c"}]
    ctrl.persist()
    assert len(save_calls) == 3, "+= should invalidate cache"

    ctrl.history[0] = {"role": "user", "content": "a2"}
    ctrl.persist()
    assert len(save_calls) == 4, "item assignment should invalidate cache"


def test_set_model_mid_turn_patches_metadata_only(tmp_path):
    """set_model can land mid-turn (the model picker is reachable while a turn
    runs), when the in-memory history may end in unanswered tool calls that
    must never reach disk. It must patch the model header WITHOUT serializing
    the in-memory messages — the same metadata-only discipline as rename and
    autoname."""
    import json

    workspace = tmp_path
    manager = SessionManager(workspace)
    store = manager.create("s6")
    deps = _make_deps(workspace, mode=Mode.ask)
    ctrl = SessionController(store, manager, deps, 100_000, 20)

    def _msg(text):
        return ModelRequest(parts=[UserPromptPart(content=text)])

    ctrl.set_history([_msg("clean")])
    ctrl.persist()

    # Mid-turn: the in-memory history has advanced past the persisted state.
    ctrl.history.append(_msg("dirty-in-flight"))
    ctrl.set_model("model-x")

    data = json.loads(store.path.read_text())
    assert data["model"] == "model-x"
    assert len(data["messages"]) == 1, "set_model full-persisted a dirty history"

    # The dirty state still reaches disk on the next real persist — the
    # metadata patch must not satisfy the persist cache.
    ctrl.persist()
    data = json.loads(store.path.read_text())
    assert len(data["messages"]) == 2


def test_abandoned_slow_persist_cannot_clobber_newer_write(tmp_path):
    """The Ctrl-C flush path abandons its persist worker after a short deadline
    but cannot stop it. If the disk stalls, that orphaned writer used to land
    its stale history *after* a newer persist and then stamp the cache with the
    *current* version — so the next persist was cache-skipped while the disk
    held stale data (the one path where an acknowledged-persisted turn could
    silently vanish). Writers must serialize and each must stamp only the
    version it actually wrote."""
    workspace = tmp_path
    manager = SessionManager(workspace)
    store = manager.create("s5")
    deps = _make_deps(workspace, mode=Mode.ask)
    ctrl = SessionController(store, manager, deps, 100_000, 20)

    original_save = store.save
    first_entered = threading.Event()
    stall = threading.Event()
    second_done = threading.Event()

    def stalling_save(*args, **kwargs):
        # The first save (the abandoned flush write) stalls until released,
        # simulating a hung disk; later saves run normally.
        if not first_entered.is_set():
            first_entered.set()
            assert stall.wait(timeout=5), "test stall never released"
            return original_save(*args, **kwargs)
        result = original_save(*args, **kwargs)
        second_done.set()
        return result

    store.save = stalling_save

    def _msg(text):
        return ModelRequest(parts=[UserPromptPart(content=text)])

    # The flush write: captures the old one-message history, then stalls.
    ctrl.set_history([_msg("old")])
    t1 = threading.Thread(target=ctrl.persist)
    t1.start()
    assert first_entered.wait(timeout=5)

    # The turn moves on: history is REPLACED (as _flush_resumable and the
    # turn-end path do) and persisted from another worker thread.
    ctrl.set_history([_msg("old"), _msg("new")])
    t2 = threading.Thread(target=ctrl.persist)
    t2.start()
    # Give the newer write a moment to land first (it does when writers are
    # unserialized — the bug); with serialized writers it just blocks here.
    second_done.wait(timeout=0.5)

    stall.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()

    # A plain persist must leave the newest history on disk — either the cache
    # is honest (disk already newest, skip is fine) or it rewrites.
    ctrl.persist()
    history, *_ = store.load()
    assert len(history) == 2, "stale abandoned write clobbered the newer persist"


def test_persist_hands_store_a_snapshot_not_the_live_history(tmp_path):
    """persist() must pass store.save a snapshot copy of the history list, not
    the live ``_VersionedHistory`` object. An abandoned persist worker (see
    ``_flush_resumable`` in runtime/controller.py) keeps running store.save's
    dump_python(history) after the Ctrl-C flush deadline expires; `_persist_lock`
    only serializes persist-vs-persist, not persist-vs-mutation of that SAME
    list object, so a subsequent turn's `history.append(...)` while the orphan
    is still iterating it can raise "list changed size during iteration" or
    write a torn snapshot. Simulate the interleaving directly at the seam:
    mutate ``ctrl.history`` from inside store.save (as a racing turn would)
    and assert the object save() received is a distinct copy, unaffected by
    the mutation."""
    workspace = tmp_path
    manager = SessionManager(workspace)
    store = manager.create("s8")
    deps = _make_deps(workspace, mode=Mode.ask)
    ctrl = SessionController(store, manager, deps, 100_000, 20)

    def _msg(text):
        return ModelRequest(parts=[UserPromptPart(content=text)])

    ctrl.set_history([_msg("first")])

    received = {}
    original_save = store.save

    def racing_save(history, *args, **kwargs):
        received["is_live_object"] = history is ctrl.history
        received["len_at_call"] = len(history)
        # A subsequent turn appending to the live history while this "save"
        # is mid-serialization — exactly what the orphaned worker races with.
        ctrl.history.append(_msg("appended-during-save"))
        received["len_after_concurrent_append"] = len(history)
        return original_save(history, *args, **kwargs)

    store.save = racing_save
    ctrl.persist()

    assert received["is_live_object"] is False, (
        "persist must hand store.save a snapshot copy, not the live history list"
    )
    assert received["len_at_call"] == 1
    assert received["len_after_concurrent_append"] == 1, (
        "the snapshot changed size when the live history was mutated concurrently"
    )
