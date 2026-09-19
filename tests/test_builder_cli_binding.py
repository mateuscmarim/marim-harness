"""The SDK builder binds external CLI execution without bootstrap or a UI."""

from pathlib import Path

import pytest

from marim_harness import HarnessBuilder
from marim_harness.config.claude_cli_model import ClaudeCliModel
from marim_harness.runtime.permissions import Mode


@pytest.mark.parametrize("model_class", [ClaudeCliModel])
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
