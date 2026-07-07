"""present_plan's UI-handoff branch: sets deps.plan, prefers on_present_plan over
ask_user, and stays correct when neither is wired (headless)."""

from types import SimpleNamespace

import pytest

from marim_harness.runtime.permissions import Mode
from marim_harness.tools.planning_tools import _PLAN_CHOICES, present_plan
from tests.conftest import _make_deps

pytestmark = pytest.mark.anyio


async def test_on_present_plan_preferred_and_sets_plan(tmp_path):
    seen = {}

    async def fake_present(summary, steps, choices):
        seen["summary"] = summary
        seen["steps"] = steps
        seen["choices"] = [c.label for c in choices]
        return "Execute hands-off (auto)"

    deps = _make_deps(tmp_path, mode=Mode.plan, on_present_plan=fake_present)
    ctx = SimpleNamespace(deps=deps)

    result = await present_plan(ctx, "Refactor the parser.", ["Extract tokenizer", "Add tests"])

    # deps.plan carries the narrative.
    assert deps.plan is not None
    assert deps.plan.summary == "Refactor the parser."
    assert deps.plan.steps == ["Extract tokenizer", "Add tests"]
    # on_present_plan was called with the summary, steps, and the canonical choices.
    assert seen["summary"] == "Refactor the parser."
    assert seen["steps"] == ["Extract tokenizer", "Add tests"]
    assert seen["choices"] == [c.label for c in _PLAN_CHOICES]
    # The chosen label flipped the mode in place.
    assert deps.workspace.mode is Mode.auto
    assert "auto" in result


async def test_falls_back_to_ask_user_when_no_present_plan(tmp_path):
    async def fake_ask(questions):
        return {questions[0].header: "Execute step-by-step (ask)"}

    deps = _make_deps(tmp_path, mode=Mode.plan, ask_user=fake_ask)  # on_present_plan unset
    ctx = SimpleNamespace(deps=deps)

    await present_plan(ctx, "s", ["one"])
    assert deps.workspace.mode is Mode.ask  # ask_user answer honored
    assert deps.plan is not None            # plan still recorded


async def test_headless_saves_and_stays_in_plan_mode(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.plan)  # neither callback wired
    ctx = SimpleNamespace(deps=deps)

    result = await present_plan(ctx, "s", ["one"])
    assert deps.workspace.mode is Mode.plan  # unchanged
    assert deps.plan is not None             # narrative still stored
    assert "plan mode" in result.lower()


async def test_dismissed_card_keeps_planning(tmp_path):
    async def fake_present(summary, steps, choices):
        return "Keep planning"

    deps = _make_deps(tmp_path, mode=Mode.plan, on_present_plan=fake_present)
    ctx = SimpleNamespace(deps=deps)

    await present_plan(ctx, "s", ["one"])
    assert deps.workspace.mode is Mode.plan  # no flip on "Keep planning"
