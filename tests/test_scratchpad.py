import os
from pathlib import Path

from marim_harness.session.store import _workspace_dir
from marim_harness.workspace.scratchpad import (
    ensure_scratchpad,
    scratchpad_base,
    scratchpad_root,
)


def test_scratchpad_root_shape():
    root = scratchpad_root(Path("/w/proj"), "sess-1", base=Path("/base"))
    assert root.name == "scratchpad"
    assert root.parent.name == "sess-1"
    slug = root.parent.parent.name
    assert slug.startswith("proj-")
    assert len(slug) == len("proj-") + 12


def test_scratchpad_root_slug_matches_session_store():
    """The workspace slug must stay in lockstep with session storage's naming
    (session/store.py::_workspace_dir), so scratchpads key the same way
    sessions do."""
    ws = Path("/w/proj")
    root = scratchpad_root(ws, "s", base=Path("/base"))
    assert root.parent.parent.name == _workspace_dir(Path("/base"), ws).name


def test_scratchpad_base_is_per_uid():
    base = scratchpad_base()
    assert f"marim-{os.getuid()}" == base.name


def test_ensure_creates_dir_with_private_base(tmp_path):
    base = tmp_path / "b"
    p = ensure_scratchpad(Path("/w/proj"), "s1", base=base)
    assert p is not None and p.is_dir()
    assert (base.stat().st_mode & 0o777) == 0o700


def test_ensure_is_idempotent_and_preserves_files(tmp_path):
    base = tmp_path / "b"
    p1 = ensure_scratchpad(Path("/w/proj"), "s1", base=base)
    (p1 / "note.txt").write_text("hi")
    p2 = ensure_scratchpad(Path("/w/proj"), "s1", base=base)
    assert p2 == p1
    assert (p2 / "note.txt").read_text() == "hi"


def test_ensure_refuses_symlink_base(tmp_path):
    """Classic /tmp squatting: a pre-existing symlink at the base must disable
    the scratchpad, not follow the link."""
    target = tmp_path / "target"
    target.mkdir()
    base = tmp_path / "b"
    base.symlink_to(target)
    assert ensure_scratchpad(Path("/w/proj"), "s1", base=base) is None
