from pathlib import Path

from marim_harness.deps import Deps
from marim_harness.permissions import Mode


def test_deps_defaults_to_ask_mode(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path)
    assert deps.mode is Mode.ask
    assert deps.request_approval is None


def test_mode_is_mutable(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path)
    deps.mode = Mode.auto
    assert deps.mode is Mode.auto


def test_deps_lsp_defaults_to_none():
    d = Deps(workspace_root=Path("."))
    assert d.lsp is None


def test_deps_lsp_can_be_set():
    d = Deps(workspace_root=Path("."))
    sentinel = object()
    d.lsp = sentinel
    assert d.lsp is sentinel
