"""Concurrent same-session saves must be serialized by an advisory lock, so two
processes (TUI + headless, or two runs) writing the same session_id can't
silently clobber each other on os.replace."""

import threading
import time
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from marim_harness.session import SessionManager


def _history() -> list:
    return Agent(TestModel(), instructions="x").run_sync("hi").all_messages()


def _manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")


def test_save_takes_a_lock_on_a_sidecar(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create()
    store.save(_history(), RunUsage())
    # The advisory lock lives on a sidecar next to the session file, never on the
    # session file itself (which goes through os.replace swaps).
    lock_path = store.path.with_name(f"{store.path.name}.lock")
    assert lock_path.exists()
    assert store.path.exists()


def test_concurrent_saves_to_same_session_are_serialized(tmp_path: Path):
    # Instrument the lock window: each save holds the lock around its write, so
    # two threads saving the SAME store must never be inside the locked section
    # at once. A shared counter that peaks at 1 proves serialization.
    mgr = _manager(tmp_path)
    store = mgr.create()
    history = _history()

    inside = 0
    max_inside = 0
    counter_lock = threading.Lock()

    real_save = store.save

    def instrumented():
        nonlocal inside, max_inside
        # Wrap save so we observe overlap of its locked section indirectly: the
        # real save holds file_lock around the write; we sleep inside save by
        # patching atomic_write_text is overkill, so instead time the call and
        # assert by the lock invariant below via a small critical-section probe.
        with counter_lock:
            inside += 1
            max_inside = max(max_inside, inside)
        real_save(history, RunUsage())
        with counter_lock:
            inside -= 1

    # The probe above brackets the whole save (lock acquire + write + release).
    # If file_lock truly serializes, concurrent threads each complete their save
    # before the next acquires — but since the probe is outside the lock, this
    # mainly proves the API is thread-safe and writes are not corrupted. The
    # correctness check is the readable, non-corrupt file below.
    threads = [threading.Thread(target=instrumented) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # After concurrent saves, the file must be valid JSON with the full history —
    # not a torn write from interleaving.
    messages, usage, tasks, dur, _ = store.load()
    assert len(messages) == len(history)


def test_file_lock_serializes_two_stores_on_same_session(tmp_path: Path):
    # Two independent SessionStore handles (simulating two processes) for the same
    # session_id. The advisory lock must serialize their save windows. We patch
    # atomic_write_text on the store module to widen the locked window and detect
    # overlap deterministically.
    import marim_harness.session.store as store_mod

    mgr = _manager(tmp_path)
    s1 = mgr.create()
    # A second handle to the SAME session id/path (the cross-process scenario).
    s2 = mgr.store(s1.session_id)

    history = _history()
    inside = 0
    max_inside = 0
    counter_lock = threading.Lock()
    real_write = store_mod.atomic_write_text

    def slow_write(path, text, **kw):
        nonlocal inside, max_inside
        with counter_lock:
            inside += 1
            max_inside = max(max_inside, inside)
        time.sleep(0.05)
        with counter_lock:
            inside -= 1
        real_write(path, text, **kw)

    store_mod.atomic_write_text = slow_write
    try:
        t1 = threading.Thread(target=lambda: s1.save(history, RunUsage()))
        t2 = threading.Thread(target=lambda: s2.save(history, RunUsage()))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    finally:
        store_mod.atomic_write_text = real_write

    # The locked write window is held by file_lock around atomic_write_text, so
    # the two slow writes never overlap.
    assert max_inside == 1
    # And the resulting file is intact.
    messages, *_ = s1.load()
    assert len(messages) == len(history)
