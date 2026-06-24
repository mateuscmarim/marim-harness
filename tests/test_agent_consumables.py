"""One-shot per-turn consumables (SessionStart hook context, finished-jobs
digest) must survive a turn whose run() raises.

``_assemble_prompt`` drains these at assembly time — it clears the SessionStart
context and the jobs finished-since-turn buffer. They are only truly "delivered"
once the run reaches the model. If the very next turn after resume fails, the
injected context (and the digest) would otherwise be lost forever. The harness
re-stashes them on the run-failure path so the next turn re-emits them; once a
round succeeds the stash is cleared, so a later approval-round failure does not
re-emit context the model already saw.
"""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from tests.conftest import _make_harness


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _fail_once_then_capture_model(captured: dict) -> FunctionModel:
    """Turn 1 raises; later turns record the latest user-prompt text seen."""
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("turn boom")
        latest = ""
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "UserPromptPart":
                    latest = str(p.content)
        captured["prompt"] = latest
        return ModelResponse(parts=[TextPart(content="ok")])

    return FunctionModel(fn)


@pytest.mark.anyio
async def test_failed_turn_re_emits_hook_context_next_turn(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    captured: dict = {}
    harness = _make_harness(_fail_once_then_capture_model(captured), deps)
    # SessionStart injected a one-shot context for the next turn.
    harness._pending_hook_context = "HOOK-CONTEXT-MARKER"

    with pytest.raises(RuntimeError):
        await harness.run_turn("first request")
    # The failed turn must not have silently eaten the injected context.
    assert harness._pending_hook_context == "HOOK-CONTEXT-MARKER"

    await harness.run_turn("second request")
    assert "HOOK-CONTEXT-MARKER" in captured["prompt"]
    # Delivered now — consumed, not carried a third time.
    assert harness._pending_hook_context is None


@pytest.mark.anyio
async def test_failed_turn_re_emits_finished_jobs_digest_next_turn(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)

    async def quick() -> str:
        return "job-result"

    job_id = deps.jobs.register("agent", "a", quick())
    await deps.jobs.wait(job_id)

    captured: dict = {}
    harness = _make_harness(_fail_once_then_capture_model(captured), deps)

    with pytest.raises(RuntimeError):
        await harness.run_turn("first request")
    # The digest was drained from deps.jobs at assembly; it must be re-stashed.
    assert harness._pending_jobs_digest is not None
    assert "background jobs finished" in harness._pending_jobs_digest

    await harness.run_turn("second request")
    assert "background jobs finished" in captured["prompt"]
    assert harness._pending_jobs_digest is None


@pytest.mark.anyio
async def test_successful_turn_consumes_hook_context(tmp_path: Path):
    """A clean turn delivers and clears the hook context (no regression)."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    captured: dict = {}

    def fn(messages, info):
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "UserPromptPart":
                    captured.setdefault("prompt", str(p.content))
        return ModelResponse(parts=[TextPart(content="ok")])

    harness = _make_harness(FunctionModel(fn), deps)
    harness._pending_hook_context = "ONE-SHOT"
    await harness.run_turn("hello")
    assert "ONE-SHOT" in captured["prompt"]
    assert harness._pending_hook_context is None
