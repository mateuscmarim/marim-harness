from pathlib import Path

from marim_harness.runtime.deps import Deps
from marim_harness.runtime.permissions import Mode


def test_deps_defaults_to_ask_mode(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path)
    assert deps.mode is Mode.ask
    assert deps.request_approval is None


def test_mode_is_mutable(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path)
    deps.mode = Mode.auto
    assert deps.mode is Mode.auto


def test_deps_has_services_container_defaulting_to_none():
    from marim_harness.runtime.deps import HarnessServices

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
    from marim_harness.runtime.deps import HarnessServices

    sentinel = object()
    d = Deps(workspace_root=Path("."), services=HarnessServices(lsp=sentinel))
    assert d.services.lsp is sentinel
    # The flat field is gone — accessing it is an attribute error.
    assert not hasattr(d, "lsp")


def test_build_services_populates_and_assigns(tmp_path):
    from marim_harness.runtime.deps import Deps, HarnessServices
    from marim_harness.runtime.harness import build_services

    deps = Deps(workspace_root=tmp_path)
    lsp = object()
    turn_hooks = object()

    class _Subs:
        async def run(self, *a, **k): ...
        async def run_background(self, *a, **k): ...

    subs = _Subs()
    services = build_services(deps, lsp=lsp, turn_hooks=turn_hooks, subagents=subs)

    assert isinstance(services, HarnessServices)
    assert services.lsp is lsp
    assert services.turn_hooks is turn_hooks
    # Use == not `is`: bound-method objects are created fresh on each attribute
    # access, so identity checks across separate accesses always fail. == compares
    # __func__ + __self__, which is exactly the "same method on same instance" check
    # the brief intends.
    assert services.run_subagent == subs.run
    assert services.run_background_agent == subs.run_background
    # The container is also installed on deps (the late binding).
    assert deps.services is services
