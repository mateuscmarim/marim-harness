from dataclasses import dataclass, field

import pytest

from marim_harness.permissions import Mode, resolve_approvals


@dataclass
class FakeCall:
    tool_call_id: str
    tool_name: str
    args: dict = field(default_factory=dict)


@dataclass
class FakeRequests:
    approvals: list = field(default_factory=list)


@pytest.fixture
def requests():
    return FakeRequests(approvals=[FakeCall("c1", "edit_file", {"path": "a.txt"})])


@pytest.mark.anyio
async def test_auto_mode_approves(requests):
    async def never(_call):  # pragma: no cover - must not be called
        raise AssertionError("request_approval must not be called in auto mode")

    results = await resolve_approvals(requests, Mode.auto, never)
    assert results.approvals["c1"] is True


@pytest.mark.anyio
async def test_plan_mode_denies(requests):
    async def never(_call):  # pragma: no cover
        raise AssertionError("request_approval must not be called in plan mode")

    results = await resolve_approvals(requests, Mode.plan, never)
    denied = results.approvals["c1"]
    assert denied is not True  # a ToolDenied instance


@pytest.mark.anyio
async def test_ask_mode_uses_callback(requests):
    seen = []

    async def approve(call):
        seen.append(call.tool_name)
        return True

    results = await resolve_approvals(requests, Mode.ask, approve)
    assert seen == ["edit_file"]
    assert results.approvals["c1"] is True
