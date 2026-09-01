"""The single-owner session claim: acquire, contend, release, inspect."""

import json
import os
from pathlib import Path

import pytest

from marim_harness.session.claim import (
    Holder,
    claim_path,
    read_holder,
    try_acquire,
)


@pytest.fixture
def session_file(tmp_path: Path) -> Path:
    path = tmp_path / "sessions" / "abc123.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    return path


def test_claim_path_is_a_sidecar_of_the_session_file(session_file: Path) -> None:
    assert claim_path(session_file) == session_file.with_name("abc123.json.claim")


def test_acquire_succeeds_on_an_unclaimed_session(session_file: Path) -> None:
    claim = try_acquire(session_file, kind="tui")
    assert claim is not None
    claim.release()


def test_second_acquire_is_refused_while_the_first_is_held(session_file: Path) -> None:
    first = try_acquire(session_file, kind="tui")
    assert first is not None
    try:
        assert try_acquire(session_file, kind="daemon") is None
    finally:
        first.release()


def test_release_makes_the_session_claimable_again(session_file: Path) -> None:
    first = try_acquire(session_file, kind="tui")
    assert first is not None
    first.release()
    second = try_acquire(session_file, kind="daemon")
    assert second is not None
    second.release()


def test_context_manager_releases_on_exit(session_file: Path) -> None:
    with try_acquire(session_file, kind="tui") as claim:
        assert claim is not None
    after = try_acquire(session_file, kind="daemon")
    assert after is not None
    after.release()


def test_holder_identity_is_readable_by_the_refused_caller(session_file: Path) -> None:
    claim = try_acquire(session_file, kind="daemon", endpoint="http://127.0.0.1:8642")
    try:
        assert try_acquire(session_file, kind="tui") is None
        holder = read_holder(session_file)
        assert holder == Holder(pid=os.getpid(), kind="daemon", endpoint="http://127.0.0.1:8642")
        assert "daemon" in holder.describe()
        assert "8642" in holder.describe()
    finally:
        claim.release()


def test_read_holder_is_none_when_no_claim_file_exists(session_file: Path) -> None:
    assert read_holder(session_file) is None


def test_read_holder_is_none_on_a_corrupt_claim_file(session_file: Path) -> None:
    claim_path(session_file).write_text("not json{{{")
    assert read_holder(session_file) is None


def test_read_holder_is_none_when_fields_are_missing(session_file: Path) -> None:
    claim_path(session_file).write_text(json.dumps({"kind": "tui"}))
    assert read_holder(session_file) is None


def test_claim_file_is_not_listed_as_a_session(session_file: Path) -> None:
    from marim_harness.session.store import SessionManager

    workspace = session_file.parent.parent / "ws"
    workspace.mkdir()
    manager = SessionManager(workspace, base_dir=session_file.parent.parent / "base")
    manager.dir.mkdir(parents=True, exist_ok=True)
    real = manager.session_path("real")
    real.write_text(json.dumps({"id": "real", "name": "real", "messages": []}))
    claim = try_acquire(real, kind="tui")
    try:
        assert [info.id for info in manager.list()] == ["real"]
    finally:
        claim.release()


def test_release_is_idempotent(session_file: Path) -> None:
    claim = try_acquire(session_file, kind="tui")
    assert claim is not None
    claim.release()
    claim.release()  # must not raise


def test_session_claimed_lives_in_claim_module_and_supervisor_reexports():
    from marim_harness.server.supervisor import SessionClaimed as ViaSupervisor
    from marim_harness.session.claim import Holder, SessionClaimed

    assert SessionClaimed is ViaSupervisor
    exc = SessionClaimed("20260831-1", Holder(pid=123, kind="tui", endpoint=None))
    assert exc.session_id == "20260831-1"
    assert exc.holder is not None and exc.holder.kind == "tui"
    assert str(exc) == "20260831-1"
