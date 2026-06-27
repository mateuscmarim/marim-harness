# tests/test_agent_checkpoints.py
# NOTE: in tests/conftest.py, _make_harness and _text_model are plain HELPER
# functions (not fixtures): _make_harness(model, deps) -> Harness, and
# _text_model() -> FunctionModel. Construct Deps explicitly, exactly as the
# existing tests/test_agent.py does (see line ~85).
import subprocess

import pytest

from marim_harness.runtime.deps import Deps
from marim_harness.runtime.permissions import Mode
from tests.conftest import _make_harness, _text_model

pytestmark = pytest.mark.anyio


async def test_turn_creates_a_checkpoint(tmp_path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_model(), deps)
    assert harness.checkpoints.list() == []
    await harness.run_turn("first user message")
    cps = harness.checkpoints.list()
    assert len(cps) == 1
    assert cps[0].history_len == 0          # captured before the turn ran
    assert cps[0].prompt_preview.startswith("first user message")


async def test_rewind_truncates_to_before_a_turn(tmp_path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_model(), deps)
    await harness.run_turn("turn one")
    after_one = list(harness.session.history)
    assert after_one  # non-empty
    await harness.run_turn("turn two")
    # Two checkpoints: index 0 (before turn one), index 1 (before turn two).
    harness.checkpoints.rewind(1)
    assert list(harness.session.history) == after_one


async def test_rewind_restores_workspace_files(tmp_path):
    # GitSnapshotter needs a git repo, so init tmp_path as the workspace. The
    # checkpoint captures the working tree at the START of the turn, so the
    # before/after mutation around run_turn — not the model — is what exercises
    # restore. Unit-level file-restore coverage lives in tests/test_snapshot.py.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_model(), deps)

    (tmp_path / "sentinel.txt").write_text("before\n")
    await harness.run_turn("first turn")               # snapshot captures "before"
    (tmp_path / "sentinel.txt").write_text("after the turn\n")
    harness.checkpoints.rewind(0)                       # back to before the turn
    assert (tmp_path / "sentinel.txt").read_text() == "before\n"
