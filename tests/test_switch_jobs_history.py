"""A /switch must not erase the switched-to session's persisted jobs history.

The harness clears the OUTGOING session's job context when switching; the bug was
doing that AFTER importing the incoming session's history, wiping the fresh import
(and re-persisting jobs=[] over the file). See harness.switch_session.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from marim_harness.runtime.harness import Harness
from marim_harness.session import SessionManager
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps

_ENTRY = {
    "id": "job-1", "kind": "agent", "label": "general: seeded",
    "status": "done", "result_tail": "seeded tail", "stream_id": "sg-seed",
    "finished_at": "2026-07-03T00:00:00+00:00",
}


@pytest.mark.anyio
async def test_switch_roundtrip_keeps_incoming_jobs_history(tmp_path: Path):
    ws = tmp_path / "ws"
    manager = SessionManager(ws, base_dir=tmp_path / "data")
    store_a = manager.create("A")
    a_id, a_path = store_a.session_id, store_a.path
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), _make_deps(ws),
        instructions="t", store=store_a, manager=manager,
    )
    # Seed A with a settled-jobs history and persist it to A's file.
    harness.deps.jobs.import_history([_ENTRY])
    harness.session.persist(force=True)
    assert json.loads(a_path.read_text())["jobs"], "precondition: A's file carries jobs"

    store_b = manager.create("B")
    b_id = store_b.session_id

    # A → B → A round-trip.
    harness.switch_session(b_id)
    assert harness.deps.jobs.history == []  # B has no jobs history
    harness.switch_session(a_id)

    # In-memory history survived the switch back (not wiped by the outgoing clear).
    assert harness.deps.jobs.history, "A's imported jobs history was erased on switch"
    assert any(j.stream_id == "sg-seed" for j in harness.deps.jobs.history)

    # And a re-persist still carries the entry — the bug re-wrote jobs=[] to disk.
    harness.session.persist(force=True)
    persisted = json.loads(a_path.read_text())["jobs"]
    assert any(e.get("stream_id") == "sg-seed" for e in persisted)
