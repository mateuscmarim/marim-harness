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
        self.deleted: list[str] = []

    def capture(self, ref: str, message: str) -> str:
        self.captured.append(ref)
        return f"commit:{ref}"

    def restore(self, commit: str) -> bool:
        self.restored.append(commit)
        return self.restore_ok

    def delete(self, ref: str) -> None:
        self.deleted.append(ref)


def test_save_writes_sidecar_under_file_lock(tmp_path: Path, monkeypatch):
    """The checkpoint sidecar write goes through ``file_lock``, matching the
    sibling session-state writers (``store.py``, ``memory.py``). Serializing the
    write keeps a concurrent same-session writer from racing the bare rename."""
    import contextlib

    from marim_harness.session import checkpoints as cp

    locked: list[Path] = []
    real = cp.file_lock

    @contextlib.contextmanager
    def spy(path):
        locked.append(Path(path))
        with real(path):
            yield

    monkeypatch.setattr(cp, "file_lock", spy)

    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    mgr.snapshot("do it")

    assert locked, "checkpoint save did not acquire the file lock"
    assert locked[-1].name == "sess.checkpoints.json"
    # The write still round-trips: a fresh manager reloads the persisted checkpoint.
    assert len(CheckpointManager(s).list()) == 1


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


def test_undo_rewind_captures_a_safety_snapshot_before_its_restore(tmp_path: Path):
    """Regression: undo_rewind's file restore deletes files created AFTER the
    rewind with no recovery path. It must first snapshot the current tree (under a
    per-session _pre_undo ref) so that post-rewind work is itself recoverable —
    mirroring how rewind() guards its own restore."""
    s = _session(tmp_path)
    snap = _FakeSnap()
    mgr = CheckpointManager(s, snap)
    mgr.snapshot("t1")
    s.set_history(["u1", "a1"])
    mgr.rewind(0)
    snap.captured.clear()
    assert mgr.undo_rewind() is True
    # A pre-undo safety snapshot was captured before the destructive restore.
    assert any(ref.endswith("sess/_pre_undo") for ref in snap.captured)


def test_undo_rewind_refuses_file_restore_when_safety_snapshot_fails(tmp_path: Path):
    """If the pre-undo safety snapshot can't be captured, undo must NOT run its
    destructive file restore (which would wipe post-rewind work with no recovery).
    The conversation half is still recovered from the in-memory stash."""

    class _FailPreUndoSnap(_FakeSnap):
        def capture(self, ref: str, message: str):
            self.captured.append(ref)
            if ref.endswith("_pre_undo"):
                return None  # safety snapshot can't be taken
            return f"commit:{ref}"

    s = _session(tmp_path)
    snap = _FailPreUndoSnap()
    mgr = CheckpointManager(s, snap)
    mgr.snapshot("t1")
    s.set_history(["u1", "a1", "u2", "a2"])
    mgr.rewind(0)
    snap.restored.clear()
    # The conversation is still recoverable from the stash...
    assert mgr.undo_rewind() is True
    assert s.history == ["u1", "a1", "u2", "a2"]
    # ...but the destructive file restore was refused (never attempted).
    assert snap.restored == []


def test_undo_rewind_without_a_prior_rewind_is_a_noop(tmp_path: Path):
    mgr = CheckpointManager(_session(tmp_path), _FakeSnap())
    assert mgr.undo_rewind() is False


def test_undo_rewind_restores_the_truncated_conversation(tmp_path: Path):
    """A rewind's history truncation is reversible: undo_rewind puts the
    conversation back (not just files) and persists the restoration."""
    s = _session(tmp_path)
    snap = _FakeSnap()
    mgr = CheckpointManager(s, snap)
    mgr.snapshot("t1")  # history_len 0 captured
    s.set_history(["u1", "a1", "u2", "a2"])
    mgr.rewind(0)
    assert s.history == []  # rewound
    persisted_before = s.persisted
    assert mgr.undo_rewind() is True
    assert s.history == ["u1", "a1", "u2", "a2"]  # conversation recovered
    assert s.persisted > persisted_before  # restoration was persisted


def test_undo_rewind_restores_conversation_for_conversation_only_rewind(tmp_path: Path):
    """A commit-less (conversation-only) rewind is now undoable too, even though it
    captured no file snapshot."""
    s = _session(tmp_path)
    snap = _FakeSnap()
    mgr = CheckpointManager(s, snap)
    mgr._checkpoints.append(
        Checkpoint(index=0, history_len=0, commit=None, created="t", prompt_preview="x")
    )
    s.set_history(["u1", "a1"])
    mgr.rewind(0)
    assert s.history == []
    assert mgr.undo_rewind() is True
    assert s.history == ["u1", "a1"]


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


# --- compaction invalidates stale checkpoints --------------------------------
#
# Checkpoint.history_len is an ABSOLUTE index into the session history. Compaction
# replaces history with a shorter, restructured list (summary + tail), so every
# pre-compaction checkpoint's index now points at the wrong boundary — rewinding
# to one would slice mid-pair and corrupt the conversation. They must be dropped.


def test_invalidate_after_compaction_drops_all_checkpoints(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s, _FakeSnap())
    s.set_history(list(range(40)))
    mgr.snapshot("t1")
    s.set_history(list(range(45)))
    mgr.snapshot("t2")
    # Compaction collapsed 45 messages down to 6; the stored history_len values
    # (40, 45) are now stale absolute indices.
    s.set_history(list(range(6)))
    mgr.invalidate_after_compaction()
    assert mgr.list() == []


def test_invalidate_after_compaction_deletes_git_refs(tmp_path: Path):
    """The orphaned checkpoints' shadow refs are deleted so they don't leak and
    block GC (mirrors _prune / rewind cleanup)."""
    s = _session(tmp_path)
    snap = _FakeSnap()
    mgr = CheckpointManager(s, snap)
    mgr.snapshot("t1")
    s.set_history(["m0"])
    mgr.snapshot("t2")
    mgr.invalidate_after_compaction()
    assert any(ref.endswith("sess/0") for ref in snap.deleted)
    assert any(ref.endswith("sess/1") for ref in snap.deleted)


def test_snapshot_after_invalidation_starts_a_fresh_index(tmp_path: Path):
    """Post-compaction the next checkpoint is a clean slate — index restarts at 0
    (its history_len is correct against the new compacted history)."""
    s = _session(tmp_path)
    mgr = CheckpointManager(s, _FakeSnap())
    mgr.snapshot("t1")
    s.set_history(["m0"])
    mgr.snapshot("t2")
    mgr.invalidate_after_compaction()
    s.set_history(["s0", "s1"])
    mgr.snapshot("fresh")
    cps = mgr.list()
    assert len(cps) == 1
    assert cps[0].index == 0
    assert cps[0].history_len == 2


def test_invalidate_after_compaction_is_a_noop_with_no_checkpoints(tmp_path: Path):
    mgr = CheckpointManager(_session(tmp_path), _FakeSnap())
    mgr.invalidate_after_compaction()  # must not raise
    assert mgr.list() == []


# --- discarding the checkpoint of a failed, output-less turn -----------------


def test_snapshot_returns_the_new_index(tmp_path: Path):
    mgr = CheckpointManager(_session(tmp_path), _FakeSnap())
    assert mgr.snapshot("a") == 0
    assert mgr.snapshot("b") == 1


def test_discard_drops_the_last_checkpoint_and_its_ref(tmp_path: Path):
    s = _session(tmp_path)
    snap = _FakeSnap()
    mgr = CheckpointManager(s, snap)
    mgr.snapshot("t1")
    idx = mgr.snapshot("t2")
    assert mgr.discard(idx) is True
    assert [c.index for c in mgr.list()] == [0]
    assert any(ref.endswith("sess/1") for ref in snap.deleted)


def test_discard_refuses_when_index_is_not_the_last(tmp_path: Path):
    """Only the most recent checkpoint is removable, so a stale index can't punch
    a hole mid-list."""
    s = _session(tmp_path)
    mgr = CheckpointManager(s, _FakeSnap())
    mgr.snapshot("t1")  # index 0
    mgr.snapshot("t2")  # index 1
    assert mgr.discard(0) is False
    assert len(mgr.list()) == 2


def test_discard_is_a_noop_with_no_checkpoints(tmp_path: Path):
    mgr = CheckpointManager(_session(tmp_path), _FakeSnap())
    assert mgr.discard(0) is False


def test_rewind_drops_later_checkpoints(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    mgr.snapshot("t1")
    s.set_history(["u1", "a1"])
    mgr.snapshot("t2")
    mgr.rewind(0)
    assert [c.index for c in mgr.list()] == [0]


class _DeleteRecordingSnap(_FakeSnap):
    """A _FakeSnap that records the refs passed to ``delete``."""

    def __init__(self, *, restore_ok: bool = True) -> None:
        super().__init__(restore_ok=restore_ok)
        self.deleted: list[str] = []

    def delete(self, ref: str) -> None:
        self.deleted.append(ref)


class _FailPreRestoreSnap(_FakeSnap):
    """Captures real commits for checkpoints, but fails the pre-restore safety
    snapshot — simulating a git failure exactly when undo-ability matters."""

    def capture(self, ref: str, message: str) -> str | None:
        self.captured.append(ref)
        if ref.endswith("_pre_restore"):
            return None  # safety snapshot can't be taken
        return f"commit:{ref}"


def _mgr_with_three(tmp_path: Path):
    """A manager with checkpoints #0, #1, #2 (each with a real commit)."""
    s = _session(tmp_path)
    snap = _DeleteRecordingSnap()
    mgr = CheckpointManager(s, snap)
    mgr.snapshot("t0")
    s.set_history(["u1", "a1"])
    mgr.snapshot("t1")
    s.set_history(["u1", "a1", "u2", "a2"])
    mgr.snapshot("t2")
    return s, snap, mgr


def test_rewind_defers_dropped_checkpoint_ref_deletion(tmp_path: Path):
    # Later checkpoints removed by a rewind are STASHED (refs kept alive), not deleted
    # immediately, so undo_rewind can restore them. Deleting on rewind (the old
    # behavior) made "rewind too far, then undo" lose #1/#2 for good.
    _s, snap, mgr = _mgr_with_three(tmp_path)
    mgr.rewind(0)
    assert [c.index for c in mgr.list()] == [0]
    assert not any(r.endswith("/1") for r in snap.deleted)
    assert not any(r.endswith("/2") for r in snap.deleted)


def test_undo_rewind_restores_dropped_checkpoints(tmp_path: Path):
    # The core fix: rewinding then undoing brings the later checkpoints back so a user
    # who rewound too far can still reach a later point.
    _s, snap, mgr = _mgr_with_three(tmp_path)
    mgr.rewind(0)
    assert [c.index for c in mgr.list()] == [0]
    assert mgr.undo_rewind() is True
    assert [c.index for c in mgr.list()] == [0, 1, 2]
    assert not any(r.endswith("/1") for r in snap.deleted)
    assert not any(r.endswith("/2") for r in snap.deleted)


def test_moving_forward_after_rewind_closes_undo_and_frees_refs(tmp_path: Path):
    # Moving forward (a new turn snapshots) instead of undoing closes the undo window:
    # the dropped checkpoints are released, their refs deleted, and undo is a no-op.
    s, snap, mgr = _mgr_with_three(tmp_path)
    mgr.rewind(0)
    s.set_history(["u1p", "a1p"])
    mgr.snapshot("moved-forward")
    assert any(r.endswith("/1") for r in snap.deleted)
    assert any(r.endswith("/2") for r in snap.deleted)
    assert mgr.undo_rewind() is False


def test_second_rewind_frees_the_first_rewinds_dropped_refs(tmp_path: Path):
    # Undo is single-level: a second rewind supersedes the first's undo, so the first
    # rewind's dropped checkpoint refs are freed (not leaked).
    _s, snap, mgr = _mgr_with_three(tmp_path)
    mgr.rewind(1)  # drops #2, keeps [0, 1]
    assert not any(r.endswith("/2") for r in snap.deleted)
    mgr.rewind(0)  # supersedes the first undo → frees #2's ref
    assert any(r.endswith("/2") for r in snap.deleted)


def test_rewind_aborts_restore_when_safety_snapshot_fails(tmp_path: Path):
    # If the pre-restore safety snapshot can't be captured, the working tree must
    # NOT be destructively restored — that would be an irreversible overwrite with
    # no undo path. The file restore is skipped and reported as failed; the
    # conversation half (already truncated) stays rewound.
    s = _session(tmp_path)
    snap = _FailPreRestoreSnap()
    mgr = CheckpointManager(s, snap)
    mgr.snapshot("t1")  # captures a real commit for the checkpoint
    s.set_history(["u1", "a1"])
    result = mgr.rewind(0)
    assert snap.restored == []  # restore was never attempted
    assert result.restored_files is False
    assert result.restore_failed is True
    assert result.pre_restore_commit is None
    assert s.history == []  # history was still rewound


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
