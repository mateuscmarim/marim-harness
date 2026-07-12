from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import HarnessServices, UIHooks
from marim_harness.runtime.harness import Harness
from tests.conftest import _make_deps, _make_harness


def test_services_default_run_workflow_none():
    assert HarnessServices().run_workflow is None


def test_ui_hooks_default_workflow_callbacks_none():
    ui = UIHooks()
    assert ui.on_workflow_spawn is None
    assert ui.on_workflow_log is None


def test_harness_wires_run_workflow_when_monty_available(tmp_path):
    """The dev environment has pydantic-monty installed (the [workflows]
    extra), so the default-enabled engine should build and services.run_workflow
    should be populated."""
    h: Harness = _make_harness(TestModel(), _make_deps(tmp_path))
    assert h.deps.services.run_workflow is not None


def test_harness_respects_workflows_disabled(tmp_path):
    h: Harness = _make_harness(TestModel(), _make_deps(tmp_path), workflows_enabled=False)
    assert h.deps.services.run_workflow is None
