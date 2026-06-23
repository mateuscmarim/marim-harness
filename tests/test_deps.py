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


def test_deps_has_services_container_defaulting_to_none():
    from marim_harness.deps import HarnessServices

    d = Deps(workspace_root=Path("."))
    assert isinstance(d.services, HarnessServices)
    assert d.services.lsp is None
    assert d.services.turn_hooks is None
    assert d.services.run_subagent is None
    assert d.services.run_background_agent is None


def test_each_deps_gets_its_own_services_container():
    a = Deps(workspace_root=Path("."))
    b = Deps(workspace_root=Path("."))
    assert a.services is not b.services


def test_lsp_handle_lives_on_services():
    from marim_harness.deps import HarnessServices

    sentinel = object()
    d = Deps(workspace_root=Path("."), services=HarnessServices(lsp=sentinel))
    assert d.services.lsp is sentinel
    # The flat field is gone — accessing it is an attribute error.
    assert not hasattr(d, "lsp")
