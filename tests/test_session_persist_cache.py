"""Persist should skip the encode/decode round-trip and the disk write when
history hasn't changed between calls — saves several MB per turn on long
sessions."""
from pydantic_ai.usage import RunUsage

from marim_harness.deps import Deps
from marim_harness.session import SessionController, SessionManager


def test_persist_skips_save_when_history_unchanged(tmp_path):
    """After the first persist, calling persist() again with no history
    mutation must not call store.save — the disk is already current."""
    workspace = tmp_path
    manager = SessionManager(workspace)
    store = manager.create("s1")
    deps = Deps(workspace_root=workspace)

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
    deps = Deps(workspace_root=workspace)

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
    deps = Deps(workspace_root=workspace)

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