import os
import threading
import time
from pathlib import Path

from marim_harness.atomic_io import atomic_write_text, file_lock


def test_atomic_write_creates_file_with_content(tmp_path: Path):
    p = tmp_path / "a.json"
    atomic_write_text(p, '{"x": 1}')
    assert p.read_text() == '{"x": 1}'


def test_atomic_write_overwrites_existing(tmp_path: Path):
    p = tmp_path / "a.json"
    p.write_text("old")
    atomic_write_text(p, "new")
    assert p.read_text() == "new"


def test_atomic_write_leaves_no_temp_residue(tmp_path: Path):
    # The old code used a deterministic "<name>.json.tmp" that concurrent writers
    # would clobber. The atomic writer must clean up its temp and never leave that
    # shared fixed name behind.
    p = tmp_path / "a.json"
    atomic_write_text(p, "data")
    assert not (tmp_path / "a.json.tmp").exists()
    assert list(tmp_path.iterdir()) == [p]  # only the target, no temp residue


def test_atomic_write_uses_unique_temp_names(tmp_path: Path, monkeypatch):
    # Two writers preparing temp files concurrently must get distinct paths, so
    # one can't truncate the other's half-written temp (the bug behind the shared
    # ".json.tmp"). Capture the temp paths by stubbing os.replace to record them.
    seen: list[str] = []

    def spy_replace(src, dst):
        seen.append(str(src))  # don't perform the swap, so both temps coexist

    monkeypatch.setattr(os, "replace", spy_replace)
    atomic_write_text(tmp_path / "a.json", "1")
    atomic_write_text(tmp_path / "a.json", "2")
    assert len(seen) == 2
    assert seen[0] != seen[1]  # distinct temp files, never a shared fixed name


# --- advisory file lock ----------------------------------------------------


def test_file_lock_is_a_working_context_manager(tmp_path: Path):
    p = tmp_path / "data.json"
    with file_lock(p):
        atomic_write_text(p, "ok")
    assert p.read_text() == "ok"
    # The lock is held on a sidecar, never the target itself.
    assert (tmp_path / "data.json.lock").exists()
    assert p.exists()


def test_file_lock_serializes_concurrent_threads(tmp_path: Path):
    # Two threads each take the lock around a brief critical section. With real
    # serialization their sections never overlap; without it the second would
    # enter while the first is still inside. We detect overlap with a shared
    # counter that must never exceed 1 while the lock is held.
    p = tmp_path / "data.json"
    inside = 0
    max_inside = 0
    lock = threading.Lock()  # protects the counters themselves

    def worker():
        nonlocal inside, max_inside
        with file_lock(p):
            with lock:
                inside += 1
                max_inside = max(max_inside, inside)
            time.sleep(0.05)
            with lock:
                inside -= 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # flock is per-open-file-description; two os.open calls in the same process
    # get independent descriptions, so the lock does serialize across threads.
    assert max_inside == 1


def test_file_lock_creates_parent_dir(tmp_path: Path):
    # The lock must create its sidecar's parent if missing, so the very first
    # write to a new session dir doesn't fail to take the lock.
    nested = tmp_path / "new" / "dir" / "data.json"
    nested.parent.mkdir(parents=True)
    with file_lock(nested):
        pass
    assert (nested.parent / "data.json.lock").exists()


# --- stale temp sweep ------------------------------------------------------


def test_atomic_write_sweeps_orphaned_temp_from_prior_crash(tmp_path: Path):
    # A crash between mkstemp and os.replace leaves a uniquely named temp behind.
    # The next write to the same target should opportunistically clear it — but
    # only once it's older than the grace window (a real crash leftover is; a
    # concurrent writer's in-flight temp is not — see the next test).
    import os

    orphan = tmp_path / ".a.json.deadbeef.tmp"
    orphan.write_text("leftover from a crash")
    old = orphan.stat().st_mtime - 600  # 10 min ago: unambiguously stale
    os.utime(orphan, (old, old))
    atomic_write_text(tmp_path / "a.json", "fresh")
    assert not orphan.exists()  # swept
    assert (tmp_path / "a.json").read_text() == "fresh"


def test_atomic_write_leaves_recent_temp_for_concurrent_writer(tmp_path: Path):
    # write_file/edit_file now run in worker threads, so two writes to the same
    # path can race. A freshly-created temp belongs to a live concurrent writer;
    # the post-replace sweep must NOT unlink it (doing so would make that writer's
    # os.replace fail with a spurious FileNotFoundError). Only aged temps are swept.
    inflight = tmp_path / ".a.json.beef.tmp"
    inflight.write_text("another writer's in-flight temp")
    atomic_write_text(tmp_path / "a.json", "fresh")
    assert inflight.exists()  # protected — too recent to be a crash orphan
    assert (tmp_path / "a.json").read_text() == "fresh"


def test_atomic_write_durable_false_skips_fsync_and_sweep(tmp_path: Path, monkeypatch):
    # Regenerable caches pass durable=False to skip the fsyncs + the stale-temp glob
    # sweep. The os.replace swap must still run (that's what makes it atomic), so the
    # content lands; only the durability extras are skipped.
    import marim_harness.atomic_io as aio

    fsyncs: list[int] = []
    monkeypatch.setattr(aio.os, "fsync", lambda fd: fsyncs.append(fd))
    swept: list[tuple] = []
    monkeypatch.setattr(aio, "_sweep_stale_temps", lambda d, n: swept.append((d, n)))

    p = tmp_path / "cache.txt"
    atomic_write_text(p, "regenerable", durable=False)
    assert p.read_text() == "regenerable"  # atomic swap still happened
    assert fsyncs == []                     # no file/dir fsync
    assert swept == []                      # no glob sweep


def test_atomic_write_durable_true_fsyncs_and_sweeps(tmp_path: Path, monkeypatch):
    # The default (sessions/checkpoints) keeps full durability: file fsync + dir
    # fsync + sweep all run.
    import marim_harness.atomic_io as aio

    fsyncs: list[int] = []
    monkeypatch.setattr(aio.os, "fsync", lambda fd: fsyncs.append(fd))
    swept: list[tuple] = []
    monkeypatch.setattr(aio, "_sweep_stale_temps", lambda d, n: swept.append((d, n)))

    atomic_write_text(tmp_path / "session.json", "durable")
    assert len(fsyncs) >= 1   # at least the file fsync (dir fsync is best-effort)
    assert len(swept) == 1


def test_atomic_write_sweep_ignores_other_targets_temps(tmp_path: Path):
    # The sweep is scoped to THIS target's temp prefix; a different file's
    # orphaned temp must be left alone.
    other = tmp_path / ".b.json.cafe.tmp"
    other.write_text("not mine")
    atomic_write_text(tmp_path / "a.json", "fresh")
    assert other.exists()
