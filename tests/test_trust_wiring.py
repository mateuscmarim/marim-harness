"""Trust resolution wired through config + bootstrap: tri-state env, store
consultation, prompt flag, and live TrustState on Deps."""

import pytest

from marim_harness.runtime.deps import Deps, TrustState, WorkspaceConfig
from marim_harness.trust import record_decision
from marim_harness.trust_surface import scan_project_surface


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)


def test_deps_carries_trust_state(tmp_path):
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path))
    assert deps.trust == TrustState(project=False, source="default", fingerprint="")


def test_config_tristate_env(monkeypatch):
    from marim_harness.config import load_config
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    assert load_config().trust_project_hooks is None
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    assert load_config().trust_project_hooks is True
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "0")
    assert load_config().trust_project_hooks is False


def test_stored_decision_flows_into_resolution(tmp_path):
    """A stored grant with a fresh fingerprint resolves trusted with no prompt —
    the exact contract bootstrap relies on (headless honors the TUI decision)."""
    from marim_harness.trust import resolve_project_trust
    (tmp_path / ".marim" / "skills" / "s").mkdir(parents=True)
    (tmp_path / ".marim" / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: s\ndescription: x\n---\n")
    surface = scan_project_surface(tmp_path)
    record_decision(tmp_path, trusted=True, fingerprint=surface.fingerprint, now="t")
    r = resolve_project_trust(tmp_path, explicit=None,
                              fingerprint=surface.fingerprint,
                              surface_empty=surface.empty)
    assert r.trusted and r.source == "store" and not r.prompt_needed
