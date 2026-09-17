"""The SDK builder binds external CLI execution without bootstrap or a UI."""

from pathlib import Path

import pytest

from marim_harness import HarnessBuilder
from marim_harness.codex.server import CodexServer
from marim_harness.config.claude_cli_model import ClaudeCliModel
from marim_harness.config.codex_cli_model import CodexCliModel
from marim_harness.runtime.permissions import Mode
from tests.fakes import fake_codex_bin, read_request_log


@pytest.mark.anyio
async def test_builder_codex_uses_workspace_and_live_mode_without_ui(tmp_path: Path):
    workspace = tmp_path / "clone"
    workspace.mkdir()
    turn = [{"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "ok"}}]
    model = CodexCliModel(
        "gpt-5.6-sol",
        server=CodexServer(binary=fake_codex_bin(tmp_path, {"turns": [turn, turn]})),
    )
    harness = HarnessBuilder(workspace=workspace, model=model).with_mode(Mode.plan).build()
    try:
        assert (await harness.run_turn("review the clone")).result == "ok"
        harness.set_mode(Mode.auto)
        assert (await harness.run_turn("apply the fix")).result == "ok"
    finally:
        await harness.aclose()

    log = read_request_log(tmp_path)
    start = next(request["params"] for request in log if request["method"] == "thread/start")
    assert start["cwd"] == str(workspace.resolve())
    assert start["sandbox"] == "read-only"
    turns = [request["params"] for request in log if request["method"] == "turn/start"]
    assert turns[0]["sandboxPolicy"]["type"] == "readOnly"
    assert turns[1]["sandboxPolicy"]["type"] == "workspaceWrite"


@pytest.mark.parametrize("model_class", [ClaudeCliModel, CodexCliModel])
def test_builder_binds_shared_cli_hooks_before_return(tmp_path: Path, model_class):
    model = model_class(None)
    harness = HarnessBuilder(workspace=tmp_path, model=model).with_mode(Mode.plan).build()

    assert model.cwd == str(tmp_path.resolve())
    assert model.mode_getter() == "plan"
    harness.set_mode(Mode.ask)
    assert model.mode_getter() == "ask"
    assert model.job_registry is harness.deps.jobs
    assert model.thinking_getter() == harness.thinking_level_id
    assert model.scratchpad_getter() is None
    assert model.session_ref_getter() == harness.session.saved_cli_thread_id
    assert model.on_session_ref is not None
    assert model.on_backend_turn is not None
    assert model.on_jobs_settled is not None
    assert model.on_activity is None
