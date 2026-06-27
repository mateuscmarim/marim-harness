"""Sub-agent spawn timing instrumentation.

A spawn's wall time splits into harness-side ``setup`` (worktree/discovery/build)
and the model's ``ttft`` (time-to-first-token). The runner logs that split at
DEBUG so a slow fan-out can be diagnosed — is it the harness or the provider? —
without guessing. These tests pin that the line is emitted (and only at DEBUG)
and that a streamed (foreground) spawn reports a real ttft, not the timing
values, which are environment-dependent.
"""

import logging
import re
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import Deps, SubAgentCallbacks
from marim_harness.runtime.permissions import Mode
from tests.conftest import _make_harness, _text_model

_SETUP_TOTAL_RE = re.compile(r"spawn 'explore' timing: setup=\d+ms ttft=(.+?) total=\d+ms")


@pytest.mark.anyio
async def test_headless_spawn_emits_timing_with_na_ttft(tmp_path: Path, caplog):
    """No UI listener and no hooks → the spawn isn't streamed, so there's no
    first-event to time: the line still emits with ttft=n/a, and the spawn
    succeeds (the probe must not force a streamed request)."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    runner = _make_harness(_text_model(), deps).subagents
    with caplog.at_level(logging.DEBUG, logger="marim_harness.subagents"):
        out = await runner.run("explore", "look around", stream_id="s1")
    assert "failed" not in out
    lines = [m for r in caplog.records if (m := r.getMessage()) and "timing" in m]
    assert len(lines) == 1
    match = _SETUP_TOTAL_RE.search(lines[0])
    assert match and match.group(1) == "n/a", lines[0]


@pytest.mark.anyio
async def test_foreground_spawn_times_a_real_ttft(tmp_path: Path, caplog):
    """A foreground spawn forwards events to the UI, so it streams and the probe
    records a concrete time-to-first-token."""
    async def _sink(_sid, _event, _usage):  # a UI listener
        return None

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto,
                callbacks=SubAgentCallbacks(on_event=_sink))
    runner = _make_harness(TestModel(call_tools=[]), deps).subagents
    with caplog.at_level(logging.DEBUG, logger="marim_harness.subagents"):
        await runner.run("explore", "look around", stream_id="s1")
    lines = [m for r in caplog.records if (m := r.getMessage()) and "timing" in m]
    assert len(lines) == 1
    match = _SETUP_TOTAL_RE.search(lines[0])
    assert match and match.group(1).endswith("ms") and match.group(1) != "n/a", lines[0]


@pytest.mark.anyio
async def test_no_timing_line_when_debug_is_off(tmp_path: Path, caplog):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    runner = _make_harness(_text_model(), deps).subagents
    with caplog.at_level(logging.INFO, logger="marim_harness.subagents"):
        await runner.run("explore", "look around", stream_id="s1")
    assert not [r for r in caplog.records if "timing" in r.getMessage()]
