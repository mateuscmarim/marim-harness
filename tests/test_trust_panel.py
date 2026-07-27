"""First-open trust prompt: mounts only when bootstrap flagged it, persists
both answers, hot-applies on grant."""

from pathlib import Path

import pytest

from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.interactions.trust_panel import TrustPanel
from marim_harness.interfaces.tui.widgets import AssistantMessage
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
