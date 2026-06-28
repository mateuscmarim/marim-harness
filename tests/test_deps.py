from pathlib import Path

from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from tests.conftest import _make_deps


def test_deps_defaults_to_ask_mode(tmp_path: Path):
    deps = _make_deps(tmp_path, mode=Mode.ask)
    assert deps.workspace.mode is Mode.ask
    assert deps.ui.request_approval is None


def test_mode_is_mutable(tmp_path: Path):
    deps = _make_deps(tmp_path, mode=Mode.ask)
    deps.workspace.mode = Mode.auto
    assert deps.workspace.mode is Mode.auto


def test_deps_has_services_container_defaulting_to_none():
    from marim_harness.runtime.deps import HarnessServices

    d = Deps(workspace=WorkspaceConfig(root=Path(".")))
    assert isinstance(d.services, HarnessServices)
    assert d.services.lsp is None
    assert d.services.turn_hooks is None
    assert d.services.run_subagent is None
    assert d.services.run_background_agent is None


def test_each_deps_gets_its_own_services_container():
    a = Deps(workspace=WorkspaceConfig(root=Path(".")))
    b = Deps(workspace=WorkspaceConfig(root=Path(".")))
    assert a.services is not b.services


def test_lsp_handle_lives_on_services():
    from marim_harness.runtime.deps import HarnessServices

    sentinel = object()
    d = Deps(workspace=WorkspaceConfig(root=Path(".")), services=HarnessServices(lsp=sentinel))
    assert d.services.lsp is sentinel
    # The flat field is gone — accessing it is an attribute error.
    assert not hasattr(d, "lsp")


def test_build_services_populates_and_assigns(tmp_path):
    from marim_harness.runtime.deps import HarnessServices
    from marim_harness.runtime.harness import build_services

    deps = _make_deps(tmp_path, mode=Mode.ask)
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


def test_uihooks_has_optional_on_mode_change():
    hooks = UIHooks()
    assert hooks.on_mode_change is None

    called = []
    hooks2 = UIHooks(on_mode_change=lambda: called.append(True))
    assert hooks2.on_mode_change is not None
    hooks2.on_mode_change()
    assert called == [True]


def test_services_has_optional_get_session_id():
    from marim_harness.runtime.deps import HarnessServices

    s = HarnessServices()
    assert s.get_session_id is None

    s2 = HarnessServices(get_session_id=lambda: "sess-1")
    assert s2.get_session_id is not None
    assert s2.get_session_id() == "sess-1"


def test_build_services_threads_get_session_id(tmp_path):
    from marim_harness.runtime.harness import build_services

    class _Subs:
        async def run(self, *a, **k): ...
        async def run_background(self, *a, **k): ...

    deps = _make_deps(tmp_path, mode=Mode.ask)
    services = build_services(
        deps,
        lsp=None,
        turn_hooks=object(),
        subagents=_Subs(),
        get_session_id=lambda: "sess-XYZ",
    )
    assert services.get_session_id is not None
    assert services.get_session_id() == "sess-XYZ"
