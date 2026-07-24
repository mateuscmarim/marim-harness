"""Read-before-edit guard: the agent must have read a file (and seen its current
content) before edit_file / write_file may modify it.

The pure tracking lives in ``ReadLedger`` (workspace.fs); the file tools thread an
optional ledger through and surface a ModelRetry when it trips. A None ledger
leaves the tools unguarded (the historical behaviour, used by direct callers)."""

import os
from pathlib import Path

import pytest
from pydantic_ai import ModelRetry

from marim_harness.tools.impl import fs
from marim_harness.workspace.fs import ReadLedger


def _edit(old: str, new: str) -> fs.Edit:
    return fs.Edit(old_string=old, new_string=new)


# --- ReadLedger (pure unit) ---

def test_ledger_reports_unread_file(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("x")
    assert ReadLedger().staleness(p) == "unread"


def test_ledger_clean_after_record(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("x")
    led = ReadLedger()
    led.record(p)
    assert led.staleness(p) is None


def test_ledger_detects_size_change(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("x")
    led = ReadLedger()
    led.record(p)
    p.write_text("xyz")
    assert led.staleness(p) == "changed"


def test_ledger_record_ignores_missing_path(tmp_path: Path):
    # A vanished/unreadable path can't be fingerprinted — record is a no-op, so
    # the file is still considered unread (the next edit reports the real error).
    led = ReadLedger()
    led.record(tmp_path / "gone.txt")
    assert led.staleness(tmp_path / "gone.txt") == "unread"


def test_ledger_staleness_none_when_recorded_file_vanishes(tmp_path: Path):
    # If the file disappears after being read, staleness() can't stat it; it
    # returns None so the normal edit path surfaces the missing-file error.
    p = tmp_path / "a.txt"
    p.write_text("x")
    led = ReadLedger()
    led.record(p)
    p.unlink()
    assert led.staleness(p) is None


def test_ledger_detects_mtime_change_at_same_size(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("abc")
    led = ReadLedger()
    led.record(p)
    st = p.stat()
    # Same byte length, but a later mtime — the content was rewritten in place.
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert led.staleness(p) == "changed"


# --- edit_file integration ---

def test_edit_without_read_is_blocked_and_leaves_file_untouched(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("hello\n")
    led = ReadLedger()
    with pytest.raises(ModelRetry) as exc:
        fs.edit_file(tmp_path, "a.txt", [_edit("hello", "bye")], ledger=led)
    assert "read" in str(exc.value).lower()
    assert p.read_text() == "hello\n"  # not modified


def test_edit_after_read_succeeds(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("hello\n")
    led = ReadLedger()
    fs.read_file(tmp_path, "a.txt", ledger=led)
    fs.edit_file(tmp_path, "a.txt", [_edit("hello", "bye")], ledger=led)
    assert p.read_text() == "bye\n"


def test_edit_blocked_when_file_changed_since_read(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("hello\n")
    led = ReadLedger()
    fs.read_file(tmp_path, "a.txt", ledger=led)
    p.write_text("hello world\n")  # external modification after the read
    with pytest.raises(ModelRetry) as exc:
        fs.edit_file(tmp_path, "a.txt", [_edit("hello", "bye")], ledger=led)
    assert "changed" in str(exc.value).lower()
    assert p.read_text() == "hello world\n"  # the edit did not apply


def test_consecutive_edits_without_reread_succeed(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("a b\n")
    led = ReadLedger()
    fs.read_file(tmp_path, "a.txt", ledger=led)
    fs.edit_file(tmp_path, "a.txt", [_edit("a", "x")], ledger=led)
    # The first edit refreshed the fingerprint, so the second isn't seen as stale.
    fs.edit_file(tmp_path, "a.txt", [_edit("b", "y")], ledger=led)
    assert p.read_text() == "x y\n"


def test_edit_without_ledger_is_unguarded(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("hello")
    fs.edit_file(tmp_path, "a.txt", [_edit("hello", "bye")])  # no ledger → no guard
    assert p.read_text() == "bye"


# --- write_file integration ---

def test_write_new_file_allowed_without_read(tmp_path: Path):
    led = ReadLedger()
    fs.write_file(tmp_path, "new.txt", "hi", ledger=led)  # target doesn't exist
    assert (tmp_path / "new.txt").read_text() == "hi"


def test_write_overwrite_existing_blocked_without_read(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("old")
    led = ReadLedger()
    with pytest.raises(ModelRetry) as exc:
        fs.write_file(tmp_path, "a.txt", "new", ledger=led)
    assert "read" in str(exc.value).lower()
    assert p.read_text() == "old"  # not overwritten


def test_write_overwrite_after_read_succeeds(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("old")
    led = ReadLedger()
    fs.read_file(tmp_path, "a.txt", ledger=led)
    fs.write_file(tmp_path, "a.txt", "new", ledger=led)
    assert p.read_text() == "new"


def test_write_then_overwrite_without_reread_succeeds(tmp_path: Path):
    led = ReadLedger()
    fs.write_file(tmp_path, "a.txt", "one", ledger=led)  # records on create
    fs.write_file(tmp_path, "a.txt", "two", ledger=led)  # overwrite not blocked
    assert (tmp_path / "a.txt").read_text() == "two"


def test_write_without_ledger_is_unguarded(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("old")
    fs.write_file(tmp_path, "a.txt", "new")  # no ledger → no guard
    assert p.read_text() == "new"


# --- provider wiring: the guard is active through the real tool + Deps ledger ---

class _Ctx:
    def __init__(self, deps):
        self.deps = deps


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_provider_edit_requires_prior_read(tmp_path: Path):
    from marim_harness.runtime.permissions import Mode
    from marim_harness.tools import edit_tools, fs_tools
    from tests.conftest import _make_deps

    (tmp_path / "m.py").write_text("x = 1\n")
    ctx = _Ctx(_make_deps(tmp_path, mode=Mode.auto))  # fresh Deps → empty ReadLedger

    with pytest.raises(ModelRetry) as exc:
        await edit_tools.edit_file(ctx, "m.py", [_edit("x = 1", "y = 2")])
    assert "read" in str(exc.value).lower()

    # After reading through the same ctx, the edit goes through.
    await fs_tools.read_file(ctx, "m.py")
    out = await edit_tools.edit_file(ctx, "m.py", [_edit("x = 1", "y = 2")])
    assert "edited m.py" in out
    assert (tmp_path / "m.py").read_text() == "y = 2\n"
