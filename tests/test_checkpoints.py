from pathlib import Path

from marim_harness.session.checkpoints import (
    Checkpoint,
    CheckpointManager,
    NullSnapshotter,
    RewindResult,
)


def test_checkpoint_roundtrips_through_dict():
    cp = Checkpoint(
        index=2, history_len=6, commit="abc123",
        created="2026-06-23T00:00:00+00:00", prompt_preview="fix the bug",
    )
    assert Checkpoint.from_dict(cp.to_dict()) == cp


def test_from_dict_tolerates_missing_optional_commit():
    d = {"index": 0, "history_len": 0, "created": "t", "prompt_preview": ""}
    assert Checkpoint.from_dict(d).commit is None


def test_null_snapshotter_captures_nothing():
    assert NullSnapshotter().capture("refs/x", "msg") is None


class _FakeStore:
    def __init__(self, path: Path, session_id: str):
        self.path = path
        self.session_id = session_id


class _FakeSession:
    """Minimal stand-in for SessionController for manager unit tests."""

    def __init__(self, store):
        self._history: list = []
        self.store = store
        self.persisted = 0

    @property
    def history(self) -> list:
        return self._history

    def set_history(self, value: list) -> None:
        self._history = value

    def persist(self, *, force: bool = False) -> None:
        self.persisted += 1


def _session(tmp_path: Path) -> _FakeSession:
    return _FakeSession(_FakeStore(tmp_path / "sess.json", "sess"))


class _FakeSnap:
    """Records the refs it captures/restores; ``restore`` reports success/failure
    so the manager's propagation can be tested without real git."""

    def __init__(self, *, restore_ok: bool = True) -> None:
        self.restore_ok = restore_ok
        self.captured: list[str] = []
        self.restored: list[str] = []

    def capture(self, ref: str, message: str) -> str:
        self.captured.append(ref)
        return f"commit:{ref}"

    def restore(self, commit: str) -> bool:
        self.restored.append(commit)
        return self.restore_ok

    def delete(self, ref: str) -> None:
        pass


def test_snapshot_records_history_length_and_preview(tmp_path: Path):
    s = _session(tmp_path)
    s.set_history(["m0", "m1"])
    mgr = CheckpointManager(s)
    mgr.snapshot("do the thing")
    cps = mgr.list()
    assert len(cps) == 1
    assert cps[0].index == 0
    assert cps[0].history_len == 2
    assert cps[0].prompt_preview == "do the thing"


def test_indices_increase_across_snapshots(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    mgr.snapshot("a")
    s.set_history(["m0"])
    mgr.snapshot("b")
    assert [c.index for c in mgr.list()] == [0, 1]


def test_rewind_truncates_history_and_persists(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    mgr.snapshot("turn-1")          # history_len 0 captured
    s.set_history(["u1", "a1", "u2", "a2"])
    result = mgr.rewind(0)
    assert isinstance(result, RewindResult)
    assert result.history_len == 0
    assert s.history == []
    assert s.persisted >= 1


def test_rewind_reports_restore_success(tmp_path: Path):
    s = _session(tmp_path)
    snap = _FakeSnap(restore_ok=True)
    mgr = CheckpointManager(s, snap)
    mgr.snapshot("t1")
    s.set_history(["u1", "a1"])
    result = mgr.rewind(0)
    assert result.restored_files is True
    assert result.restore_failed is False


def test_rewind_reports_restore_failure_instead_of_false_success(tmp_path: Path):
    """Regression: a failed git restore was reported as restored_files=True."""
    s = _session(tmp_path)
    snap = _FakeSnap(restore_ok=False)
    mgr = CheckpointManager(s, snap)
    mgr.snapshot("t1")
    s.set_history(["u1", "a1"])
    result = mgr.rewind(0)
    assert result.restored_files is False
    assert result.restore_failed is True


def test_rewind_captures_per_session_pre_restore_ref(tmp_path: Path):
    """The pre-restore safety snapshot is namespaced by session id, so a rewind in
    one session can't clobber another's recovery point."""
    s = _session(tmp_path)  # session_id == "sess"
    snap = _FakeSnap()
    mgr = CheckpointManager(s, snap)
    mgr.snapshot("t1")
    s.set_history(["u1", "a1"])
    result = mgr.rewind(0)
    assert any(ref.endswith("sess/_pre_restore") for ref in snap.captured)
    assert result.pre_restore_commit is not None


def test_undo_rewind_restores_the_pre_restore_snapshot(tmp_path: Path):
    """The safety net is reachable: undo_rewind restores the pre-rewind file state."""
    s = _session(tmp_path)
    snap = _FakeSnap()
    mgr = CheckpointManager(s, snap)
    mgr.snapshot("t1")
    s.set_history(["u1", "a1"])
    result = mgr.rewind(0)
    snap.restored.clear()
    assert mgr.undo_rewind() is True
    assert result.pre_restore_commit in snap.restored


def test_undo_rewind_without_a_prior_rewind_is_a_noop(tmp_path: Path):
    mgr = CheckpointManager(_session(tmp_path), _FakeSnap())
    assert mgr.undo_rewind() is False


def test_rewind_conversation_only_checkpoint_has_no_pre_restore(tmp_path: Path):
    """A checkpoint with no commit (NullSnapshotter / conversation-only) neither
    restores files nor captures a pre-restore snapshot."""
    s = _session(tmp_path)
    snap = _FakeSnap()
    mgr = CheckpointManager(s, snap)
    # NullSnapshotter path: force a commit-less checkpoint.
    from marim_harness.session.checkpoints import Checkpoint

    mgr._checkpoints.append(
        Checkpoint(index=0, history_len=0, commit=None, created="t", prompt_preview="x")
    )
    s.set_history(["u1", "a1"])
    result = mgr.rewind(0)
    assert result.restored_files is False
    assert result.restore_failed is False
    assert result.pre_restore_commit is None
    assert snap.captured == []


def test_rewind_drops_later_checkpoints(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    mgr.snapshot("t1")
    s.set_history(["u1", "a1"])
    mgr.snapshot("t2")
    mgr.rewind(0)
    assert [c.index for c in mgr.list()] == [0]


def test_rewind_unknown_index_raises(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    import pytest
    with pytest.raises(KeyError):
        mgr.rewind(99)


def test_checkpoints_persist_to_sidecar_and_reload(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    mgr.snapshot("kept across reload")
    mgr2 = CheckpointManager(s)
    mgr2.reload()
    assert mgr2.list()[0].prompt_preview == "kept across reload"


def test_clear_empties_checkpoints(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    mgr.snapshot("t1")
    mgr.clear()
    assert mgr.list() == []


def test_limit_prunes_oldest(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s, limit=2)
    for i in range(4):
        s.set_history(list(range(i)))
        mgr.snapshot(f"t{i}")
    indices = [c.index for c in mgr.list()]
    assert indices == [2, 3]  # oldest two pruned, newest kept
