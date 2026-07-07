"""The CurrentPlan holder and the on_present_plan callback field on Deps/UIHooks."""

from pathlib import Path

from marim_harness.runtime.deps import CurrentPlan, Deps, UIHooks, WorkspaceConfig


def test_current_plan_holds_narrative():
    plan = CurrentPlan(summary="do the thing", steps=["a", "b"], path="/tmp/p.md")
    assert plan.summary == "do the thing"
    assert plan.steps == ["a", "b"]
    assert plan.path == "/tmp/p.md"


def test_deps_plan_defaults_none_and_uihooks_callback_defaults_none():
    deps = Deps(workspace=WorkspaceConfig(root=Path("/tmp")))
    assert deps.plan is None
    assert deps.ui.on_present_plan is None


def test_deps_plan_is_assignable():
    deps = Deps(workspace=WorkspaceConfig(root=Path("/tmp")))
    deps.plan = CurrentPlan(summary="s", steps=["x"], path=None)
    assert deps.plan.steps == ["x"]
    assert isinstance(deps.ui, UIHooks)
