"""Live smoke against the real `claude` CLI (subscription quota!).

Runs only with MARIM_LIVE_CLAUDE=1 — never set it in CI or by default:

    MARIM_LIVE_CLAUDE=1 uv run pytest --no-cov -n 0 tests/test_claude_cli_live.py -v

Drives the real ``ClaudeCliModel`` (``marim_harness.config.claude_cli_model``)
exactly as ``tests/test_claude_cli_model.py`` does against the fake: build it,
set ``cwd``/``mode_getter``, call ``request()`` with a plain ``ModelRequest``
history and a fresh ``ModelRequestParameters()``, and ``aclose()`` when done.
The CLI session id lives on ``model.session_id`` (the same seam the fake-backed
tests assert on) and is expected to survive an ``aclose()`` + resume, since
that's exactly what ``_ensure_process`` does with the persisted/live id.
"""

from __future__ import annotations

import os
import shutil

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from marim_harness.config.claude_cli_model import ClaudeCliModel

pytestmark = pytest.mark.skipif(
    os.environ.get("MARIM_LIVE_CLAUDE") != "1" or shutil.which("claude") is None,
    reason="live claude smoke: set MARIM_LIVE_CLAUDE=1 with a logged-in `claude` on PATH",
)


def _user(text: str) -> list:
    return [ModelRequest(parts=[UserPromptPart(content=text)], instructions="Answer tersely.")]


@pytest.fixture
def model(tmp_path):
    # The tests close the model themselves (in `finally`); the fixture only builds it.
    m = ClaudeCliModel(os.environ.get("MARIM_CLAUDE_CLI_MODEL") or "haiku")
    m.cwd = str(tmp_path)
    m.mode_getter = lambda: "auto"
    return m


@pytest.mark.anyio
async def test_two_turns_on_one_process_and_resume_after_close(model, tmp_path):
    try:
        r1 = await model.request(_user("Reply with exactly: PING"), None, ModelRequestParameters())
        assert "PING" in r1.parts[0].content
        sid = model.session_id
        assert sid
        history = _user("Reply with exactly: PING") + [
            r1,
            ModelRequest(parts=[UserPromptPart(content="Now reply with exactly: PONG")]),
        ]
        r2 = await model.request(history, None, ModelRequestParameters())
        assert "PONG" in r2.parts[0].content
        await model.aclose()
        history = history + [
            r2,
            ModelRequest(
                parts=[UserPromptPart(content="What two words did you reply with, in order?")]
            ),
        ]
        r3 = await model.request(history, None, ModelRequestParameters())
        assert "PING" in r3.parts[0].content and "PONG" in r3.parts[0].content
        assert model.session_id == sid
    finally:
        await model.aclose()


@pytest.mark.anyio
async def test_plan_mode_denies_a_write(model, tmp_path):
    model.mode_getter = lambda: "plan"
    try:
        resp = await model.request(
            _user(
                f"Create the file {tmp_path}/x.txt containing 'hi' using the "
                "Write tool, then say whether it worked."
            ),
            None,
            ModelRequestParameters(),
        )
    finally:
        await model.aclose()
    assert not (tmp_path / "x.txt").exists()
    assert resp.parts[0].content


@pytest.mark.anyio
async def test_auto_mode_writes_inside_the_workspace(model, tmp_path):
    try:
        await model.request(
            _user(
                f"Create the file {tmp_path}/y.txt containing exactly 'hi' using the Write tool."
            ),
            None,
            ModelRequestParameters(),
        )
    finally:
        await model.aclose()
    assert (tmp_path / "y.txt").read_text().strip() == "hi"
