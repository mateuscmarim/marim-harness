import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from marim_harness.runtime.deps import Deps, HarnessServices, WorkspaceConfig
from marim_harness.session.store import _workspace_dir
from marim_harness.tools import edit_tools, fs_tools
from marim_harness.tools.impl import fs
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


def _ctx(ws: Path, scratch: Path | None) -> SimpleNamespace:
    deps = Deps(workspace=WorkspaceConfig(root=ws))
    deps.services = HarnessServices(
        get_scratchpad=(lambda: scratch) if scratch is not None else None
    )
    return SimpleNamespace(deps=deps)


def test_scratch_roots_empty_without_getter(tmp_path):
    assert fs_tools._scratch_roots(_ctx(tmp_path, None)) == ()


def test_scratch_roots_empty_when_getter_returns_none(tmp_path):
    ctx = _ctx(tmp_path, None)
    ctx.deps.services = HarnessServices(get_scratchpad=lambda: None)
    assert fs_tools._scratch_roots(ctx) == ()


@pytest.mark.anyio
async def test_write_tool_reaches_scratchpad(tmp_path):
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    ctx = _ctx(ws, scratch)
    await edit_tools.write_file(ctx, str(scratch / "note.txt"), "hi")
    assert (scratch / "note.txt").read_text() == "hi"


@pytest.mark.anyio
async def test_edit_tool_reaches_scratchpad_after_read(tmp_path):
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    (scratch / "note.txt").write_text("hello")
    ctx = _ctx(ws, scratch)
    # read first: the ReadLedger guard applies to scratchpad files too.
    fs_tools.read_file(ctx, str(scratch / "note.txt"))
    await edit_tools.edit_file(
        ctx,
        str(scratch / "note.txt"),
        [fs.Edit(old_string="hello", new_string="goodbye")],
    )
    assert (scratch / "note.txt").read_text() == "goodbye"


def test_read_tool_reaches_scratchpad(tmp_path):
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    (scratch / "data.txt").write_text("payload")
    out = fs_tools.read_file(_ctx(ws, scratch), str(scratch / "data.txt"))
    assert "payload" in out


def test_scratchpad_fns_reexported_from_workspace_package():
    """The workspace package re-exports every submodule's public API;
    scratchpad joins that convention."""
    from marim_harness import workspace

    assert workspace.ensure_scratchpad is ensure_scratchpad
    assert workspace.scratchpad_base is scratchpad_base
    assert workspace.scratchpad_root is scratchpad_root


def test_ensure_tightens_loose_preexisting_base(tmp_path):
    """A pre-existing base with group/other access (created by an older
    version, or a fumbled manual mkdir) is chmodded back to 0700 rather than
    trusted as-is — ownership is already verified, so tightening is safe."""
    base = tmp_path / "b"
    base.mkdir(mode=0o755)
    p = ensure_scratchpad(Path("/w/proj"), "s1", base=base)
    assert p is not None
    assert (base.stat().st_mode & 0o777) == 0o700
