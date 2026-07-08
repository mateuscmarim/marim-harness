from pathlib import Path
from types import SimpleNamespace

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.tools import memory_tools
from marim_harness.workspace.skills import discover_skills, find_skill


def _ctx(workspace: Path, **kw) -> SimpleNamespace:
    return SimpleNamespace(deps=Deps(workspace=WorkspaceConfig(root=workspace, **kw)))


def test_memory_root_overrides_default_scopes(tmp_path: Path):
    store = tmp_path / "memstore"
    ctx = _ctx(tmp_path / "ws", memory_root=store)
    g = memory_tools.resolve_scope(ctx, "global")
    p = memory_tools.resolve_scope(ctx, "project")
    assert g.root == store / "global"
    assert p.root == store / "project"


def test_memory_default_scopes_unchanged(tmp_path: Path):
    ctx = _ctx(tmp_path / "ws")
    p = memory_tools.resolve_scope(ctx, "project")
    assert p.root == tmp_path / "ws" / ".marim" / "memory"


def _write_skill(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: a test skill\n---\nbody of {name}\n"
    )


def test_explicit_skill_dirs_replace_discovery(tmp_path: Path):
    explicit = tmp_path / "sk"
    _write_skill(explicit, "alpha")
    ws = tmp_path / "ws"
    (ws / ".marim" / "skills").mkdir(parents=True)
    _write_skill(ws / ".marim" / "skills", "hidden")
    found = discover_skills(ws, dirs=(explicit,))
    assert [s.name for s in found] == ["alpha"]
    assert find_skill(ws, "alpha", dirs=(explicit,)) is not None
    assert find_skill(ws, "hidden", dirs=(explicit,)) is None
