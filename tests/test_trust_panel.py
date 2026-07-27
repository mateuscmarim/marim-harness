"""First-open trust prompt: mounts only when bootstrap flagged it, persists
both answers, hot-applies on grant."""

from pathlib import Path

import pytest

from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.commands import dispatch
from marim_harness.interfaces.tui.interactions.trust_panel import TrustPanel
from marim_harness.interfaces.tui.widgets import AssistantMessage
from marim_harness.runtime.deps import TrustState
from marim_harness.trust import stored_decision
from marim_harness.trust_surface import ProjectSurface
from tests.conftest import _make_deps, _make_harness, _text_model

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    # The trust store lives under $XDG_STATE_HOME; isolate it per-test (xdist
    # runs suites in parallel) and make sure no developer env var forces a
    # trust decision that would short-circuit the prompt-needed path.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)


def _surface() -> ProjectSurface:
    return ProjectSurface(
        hook_events=["SessionStart"],
        mcp_servers=["deploy"],
        fingerprint="fp1",
    )


def _app_with_prompt(tmp_path: Path, surface: ProjectSurface | None) -> HarnessApp:
    """Build a real HarnessApp/Harness (mirrors test_app_present_plan.py's
    fixture) with a fake apply_project_trust so the panel/flow can be
    exercised without a real hooks/MCP hot-reload."""
    deps = _make_deps(tmp_path)
    harness = _make_harness(_text_model(), deps)
    harness.trust_prompt = surface
    harness.apply_called = False

    async def _fake_apply() -> None:
        harness.apply_called = True

    harness.apply_project_trust = _fake_apply  # type: ignore[method-assign]
    return HarnessApp(harness)


async def _settle_until(pilot, predicate, attempts: int = 50) -> None:
    for _ in range(attempts):
        await pilot.pause()
        if predicate():
            return
    raise AssertionError("condition never became true")


async def test_panel_mounts_when_prompt_pending(tmp_path):
    app = _app_with_prompt(tmp_path, _surface())
    async with app.run_test() as pilot:
        await _settle_until(pilot, lambda: bool(app.query(TrustPanel)))
        assert app.query(TrustPanel)


async def test_no_panel_when_no_prompt(tmp_path):
    app = _app_with_prompt(tmp_path, None)
    async with app.run_test() as pilot:
        for _ in range(10):
            await pilot.pause()
        assert not app.query(TrustPanel)


async def test_trust_key_persists_applies_and_confirms(tmp_path):
    app = _app_with_prompt(tmp_path, _surface())
    async with app.run_test() as pilot:
        await _settle_until(pilot, lambda: bool(app.query(TrustPanel)))
        await pilot.press("t")
        await _settle_until(pilot, lambda: not app.query(TrustPanel))

        decision = stored_decision(tmp_path)
        assert decision is not None
        assert decision.trusted is True
        assert app.harness.apply_called is True

        text = " ".join(w.text for w in app.query(AssistantMessage))
        assert "trusted" in text.lower()


async def test_apply_failure_keeps_decision_and_surfaces_error(tmp_path):
    """A raised apply_project_trust must not vanish: the decision stays
    persisted trusted=True (the user consented), an error line lands in the
    transcript, and the app doesn't crash despite exit_on_error=False."""
    app = _app_with_prompt(tmp_path, _surface())

    async def _boom() -> None:
        raise RuntimeError("boom")

    app.harness.apply_project_trust = _boom  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await _settle_until(pilot, lambda: bool(app.query(TrustPanel)))
        await pilot.press("t")
        await _settle_until(pilot, lambda: not app.query(TrustPanel))

        decision = stored_decision(tmp_path)
        assert decision is not None
        assert decision.trusted is True

        text = " ".join(w.text for w in app.query(AssistantMessage))
        assert "boom" in text.lower()


async def test_decline_key_persists_and_notices(tmp_path):
    app = _app_with_prompt(tmp_path, _surface())
    async with app.run_test() as pilot:
        await _settle_until(pilot, lambda: bool(app.query(TrustPanel)))
        await pilot.press("d")
        await _settle_until(pilot, lambda: not app.query(TrustPanel))

        decision = stored_decision(tmp_path)
        assert decision is not None
        assert decision.trusted is False
        assert app.harness.apply_called is False

        text = " ".join(w.text for w in app.query(AssistantMessage))
        assert "/trust" in text


def _app_for_trust_cmd(
    tmp_path: Path, *, trusted: bool = False, source: str = "default",
    surface: ProjectSurface | None = None,
) -> HarnessApp:
    """Build a real HarnessApp/Harness for exercising the `/trust` command
    (as opposed to the first-open panel above): a stubbed apply/revoke so the
    hot-apply path is observable without a real hooks/MCP reload, and an
    explicit TrustState/project_surface so status/on/off can be asserted
    against a known starting point."""
    deps = _make_deps(tmp_path, trust=TrustState(project=trusted, source=source))
    harness = _make_harness(_text_model(), deps)
    harness.project_surface = surface
    harness.apply_called = False
    harness.revoke_called = False

    async def _fake_apply() -> None:
        harness.apply_called = True
        harness.deps.trust.project = True

    def _fake_revoke() -> None:
        harness.revoke_called = True
        harness.deps.trust.project = False

    harness.apply_project_trust = _fake_apply  # type: ignore[method-assign]
    harness.revoke_project_trust = _fake_revoke  # type: ignore[method-assign]
    return HarnessApp(harness)


async def test_trust_status_reports_state_and_surface(tmp_path):
    app = _app_for_trust_cmd(tmp_path, trusted=False, source="default", surface=_surface())
    async with app.run_test() as pilot:
        await pilot.pause()
        await dispatch(app, "/trust")
        await pilot.pause()
        text = " ".join(w.text for w in app.query(AssistantMessage))
    assert "untrusted" in text.lower()
    assert "default" in text
    assert "hooks" in text  # from the surface summary


async def test_trust_on_persists_and_applies(tmp_path):
    app = _app_for_trust_cmd(tmp_path, trusted=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        await dispatch(app, "/trust on")
        await pilot.pause()
        text = " ".join(w.text for w in app.query(AssistantMessage))
    decision = stored_decision(tmp_path)
    assert decision is not None
    assert decision.trusted is True
    assert app.harness.apply_called is True
    assert "trusted" in text.lower()


async def test_trust_off_persists_and_warns_restart(tmp_path):
    app = _app_for_trust_cmd(tmp_path, trusted=True, source="store")
    async with app.run_test() as pilot:
        await pilot.pause()
        await dispatch(app, "/trust off")
        await pilot.pause()
        text = " ".join(w.text for w in app.query(AssistantMessage))
    decision = stored_decision(tmp_path)
    assert decision is not None
    assert decision.trusted is False
    assert app.harness.revoke_called is True
    assert "restart" in text.lower()


async def test_trust_rejects_unknown_arg(tmp_path):
    app = _app_for_trust_cmd(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await dispatch(app, "/trust bananas")
        await pilot.pause()
        text = " ".join(w.text for w in app.query(AssistantMessage))
    assert stored_decision(tmp_path) is None
    assert "usage" in text.lower()
