"""Depth field on Deps — the foundation for nested sub-agent tracking."""

from pathlib import Path

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode


def _make_deps(tmp_path: Path, **kw) -> Deps:
    return Deps(
        workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto),
        **kw,
    )


def test_subagent_depth_defaults_to_zero():
    deps = _make_deps(Path("/tmp"))
    assert deps.subagent_depth == 0


def test_subagent_depth_can_be_set():
    deps = _make_deps(Path("/tmp"), subagent_depth=1)
    assert deps.subagent_depth == 1


def test_subagent_depth_increments_via_replace():
    deps = _make_deps(Path("/tmp"))
    child = deps.replace(subagent_depth=deps.subagent_depth + 1)
    assert child.subagent_depth == 1
    assert deps.subagent_depth == 0  # original unchanged
