"""Harness-level advisor wiring: config default -> live seam, the session
"off" sentinel beating the env default, the live setter's persist rules, the
builder front door, and the bootstrap env pass-through."""

from pydantic_ai.models.test import TestModel

from marim_harness.advisor import ADVISOR_OFF
from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionManager
from marim_harness.tools.provider import BuiltinToolProvider


def _harness(tmp_path, **kwargs) -> Harness:
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    return Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, "Be helpful.", **kwargs
    )


def test_config_default_activates_the_seam(tmp_path):
    h = _harness(tmp_path, advisor_model="openrouter:opus", advisor_max_uses=2)
    assert h.advisor_model_id == "openrouter:opus"
    assert h.deps.services.advise is not None
    assert h.deps.advisor_max_uses == 2


def test_unconfigured_leaves_the_seam_none(tmp_path):
    h = _harness(tmp_path)
    assert h.advisor_model_id is None
    assert h.deps.services.advise is None
    assert h.deps.advisor_max_uses is None


def test_session_off_sentinel_beats_config_default(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.advisor_model = ADVISOR_OFF
    h = _harness(tmp_path, store=store, manager=manager, advisor_model="openrouter:opus")
    assert h.advisor_model_id is None
    assert h.deps.services.advise is None


def test_session_slug_beats_config_default(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.advisor_model = "local:small"
    h = _harness(tmp_path, store=store, manager=manager, advisor_model="openrouter:opus")
    assert h.advisor_model_id == "local:small"


def test_set_advisor_model_switches_and_persists(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(tmp_path, store=store, manager=manager)
    h.set_advisor_model("openrouter:opus")
    assert h.advisor_model_id == "openrouter:opus"
    assert h.deps.services.advise is not None
    assert store.advisor_model == "openrouter:opus"


def test_set_advisor_model_none_disables_and_persists_sentinel(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(
        tmp_path, store=store, manager=manager, advisor_model="openrouter:opus"
    )
    h.set_advisor_model(None)
    assert h.advisor_model_id is None
    assert h.deps.services.advise is None
    assert store.advisor_model == ADVISOR_OFF


def test_builder_with_advisor(tmp_path):
    from marim_harness.runtime.builder import HarnessBuilder

    h = (
        HarnessBuilder(workspace=tmp_path, model=TestModel(call_tools=[]))
        .with_advisor("openrouter:opus", max_tokens=512, max_uses=2)
        .build()
    )
    assert h.advisor_model_id == "openrouter:opus"
    assert h.deps.services.advise is not None
    assert h.deps.advisor_max_uses == 2


def test_bootstrap_passes_advisor_env(monkeypatch, tmp_path):
    from marim_harness.runtime.bootstrap import build_harness

    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_ADVISOR_MODEL", "openrouter:anthropic/claude-opus-4.8")
    harness = build_harness(tmp_path)
    assert harness.advisor_model_id == "openrouter:anthropic/claude-opus-4.8"
    assert harness.deps.services.advise is not None
