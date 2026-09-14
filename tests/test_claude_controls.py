"""``claude/controls.py`` — the pure mode/model/thinking → control-request
mapping — and ``ClaudeProcess``'s thin wrappers over it, driven against the
scripted fake ``claude`` (a real subprocess speaking the control protocol),
whose stdin log shows exactly which control requests went out."""

from __future__ import annotations

from pathlib import Path

import pytest

from marim_harness.claude.controls import (
    ControlState,
    ThinkingControls,
    claude_permission_mode,
    thinking_controls,
)
from marim_harness.claude.process import ClaudeProcess, ProcessOptions
from marim_harness.claude.protocol import ControlError, ProcessClosed
from marim_harness.runtime.permissions import Mode
from marim_harness.thinking import THINKING_LEVELS
from tests.fakes import fake_claude_bin, read_claude_log

pytestmark = pytest.mark.anyio

# --- pure mapping -------------------------------------------------------------------


def test_permission_mode_never_bypasses_the_broker():
    # plan is Claude's own plan mode; auto and ask both leave the CLI asking
    # marim before every gated tool (the broker tells them apart).
    assert claude_permission_mode(Mode.plan) == "plan"
    assert claude_permission_mode(Mode.auto) == "default"
    assert claude_permission_mode(Mode.ask) == "default"
    assert {claude_permission_mode(m) for m in Mode} <= {"plan", "default"}


def test_every_thinking_level_maps_and_grows_with_the_level():
    controls = [thinking_controls(level) for level in THINKING_LEVELS]
    assert all(c is not None for c in controls)
    # off: no budget for token-budget models; adaptive ones cannot switch
    # thinking off, so they get the lowest effort instead of the default.
    assert thinking_controls("off") == ThinkingControls(budget=0, effort="low")
    budgets = [c.budget for c in controls if c is not None]
    assert budgets == sorted(budgets) and budgets[0] == 0 and budgets[1] >= 1024
    # Effort names are the CLI's vocabulary; the top four spell marim's levels.
    assert [c.effort for c in controls if c is not None] == [
        "low",
        "low",
        "low",
        "medium",
        "high",
        "xhigh",
    ]


def test_unset_or_unknown_thinking_level_means_no_opinion():
    assert thinking_controls(None) is None
    assert thinking_controls("") is None
    assert thinking_controls("ultra") is None


def test_control_state_starts_needing_a_mode_but_not_the_launch_model():
    state = ControlState(model="sonnet")
    assert state.mode is None and state.model == "sonnet" and state.thinking is None


# --- process wrappers -----------------------------------------------------------------


def _controls_sent(tmp_path: Path) -> list[dict]:
    return [
        m["request"]
        for m in read_claude_log(tmp_path)
        if m.get("type") == "control_request" and m["request"]["subtype"] != "initialize"
    ]


async def _started(tmp_path: Path, scenario: dict, **opts) -> ClaudeProcess:
    binary = fake_claude_bin(tmp_path, scenario)
    process = ClaudeProcess(ProcessOptions(binary=binary, cwd=str(tmp_path), **opts))
    await process.start()
    return process


async def test_wrappers_send_the_wire_shapes_and_record_the_acknowledgement(tmp_path):
    process = await _started(tmp_path, {}, model="sonnet")
    try:
        assert process.controls == ControlState(mode=None, model="sonnet", thinking=None)
        await process.set_mode(Mode.plan)
        await process.set_model("opus")
        await process.set_thinking(ThinkingControls(budget=16384, effort="medium"))
    finally:
        await process.aclose()
    assert _controls_sent(tmp_path) == [
        {"subtype": "set_permission_mode", "mode": "plan"},
        {"subtype": "set_model", "model": "opus"},
        {"subtype": "set_max_thinking_tokens", "max_thinking_tokens": 16384},
        {"subtype": "apply_flag_settings", "settings": {"effortLevel": "medium"}},
    ]
    assert process.controls == ControlState(
        mode=Mode.plan, model="opus", thinking=ThinkingControls(budget=16384, effort="medium")
    )


async def test_resetting_model_and_thinking_sends_nulls(tmp_path):
    process = await _started(tmp_path, {}, model="sonnet")
    try:
        await process.set_model(None)
        await process.set_thinking(None)
    finally:
        await process.aclose()
    assert _controls_sent(tmp_path) == [
        {"subtype": "set_model", "model": None},
        {"subtype": "set_max_thinking_tokens", "max_thinking_tokens": None},
        {"subtype": "apply_flag_settings", "settings": {"effortLevel": None}},
    ]
    assert process.controls.model is None and process.controls.thinking is None


async def test_a_rejected_model_raises_and_leaves_the_state_untouched(tmp_path):
    process = await _started(tmp_path, {"reject_models": ["bogus"]}, model="sonnet")
    try:
        with pytest.raises(ControlError, match="Model 'bogus' not found"):
            await process.set_model("bogus")
    finally:
        await process.aclose()
    assert process.controls.model == "sonnet"


async def test_wrappers_refuse_a_closed_process(tmp_path):
    process = await _started(tmp_path, {})
    await process.aclose()
    with pytest.raises(ProcessClosed):
        await process.set_mode(Mode.auto)
    with pytest.raises(ProcessClosed):
        await process.set_model("opus")
    with pytest.raises(ProcessClosed):
        await process.set_thinking(None)
