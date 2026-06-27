"""Active-time accounting on the SessionController. The open segment must be
folded into ``duration_seconds`` exactly once at shutdown — neither lost to a
cache-skipped persist nor double-counted by persist recomputing its own elapsed."""

from marim_harness.runtime.deps import Deps
from marim_harness.session import SessionController, SessionManager
from marim_harness.session import ctrl as ctrl_mod


def _ctrl_with_tracked_store(tmp_path, name):
    manager = SessionManager(tmp_path)
    store = manager.create(name)
    deps = Deps(workspace_root=tmp_path)
    saved_durations: list = []
    original_save = store.save

    def tracking_save(*args, **kwargs):
        saved_durations.append(kwargs.get("duration_seconds"))
        return original_save(*args, **kwargs)

    store.save = tracking_save
    return SessionController(store, manager, deps, 100_000, 20), saved_durations


def test_finalize_folds_segment_once_and_persists_on_idle_exit(tmp_path, monkeypatch):
    ctrl, saved = _ctrl_with_tracked_store(tmp_path, "dur1")
    ctrl.set_history([{"role": "user", "content": "hi"}])
    ctrl.persist()  # a turn persisted; history now == last-persisted version

    # Open a 5-second active segment under a controlled clock.
    clock = [1000.0]
    monkeypatch.setattr(ctrl_mod.time, "monotonic", lambda: clock[0])
    ctrl.duration_seconds = 10.0
    ctrl._segment_start = 1000.0
    clock[0] = 1005.0  # 5s elapsed

    ctrl.finalize_active_time()
    assert ctrl.duration_seconds == 15.0  # 10 prior + 5 this segment
    assert ctrl._segment_start == 0.0  # clock stopped

    # Idle exit: history unchanged since the last persist. Without force this
    # would cache-skip and the 5s would be lost; with the close+force it lands
    # exactly once (15, not the 20 a double-count would produce).
    ctrl.persist(force=True)
    assert saved[-1] == 15.0


def test_finalize_is_a_noop_without_an_open_segment(tmp_path):
    ctrl, _ = _ctrl_with_tracked_store(tmp_path, "dur2")
    ctrl.duration_seconds = 7.0
    ctrl._segment_start = 0.0  # never started / already finalized
    ctrl.finalize_active_time()
    assert ctrl.duration_seconds == 7.0
    assert ctrl._segment_start == 0.0
