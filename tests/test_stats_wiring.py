from __future__ import annotations

from pathlib import Path

from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from marim_harness import HarnessBuilder
from marim_harness.stats.ledger import default_stats_base


def test_with_sessions_writes_stats(tmp_path: Path):
    sessions = tmp_path / "sessions"
    h = (HarnessBuilder(workspace=tmp_path / "ws", model=TestModel())
         .with_sessions(dir=sessions).build())
    assert h.session.stats_recorder is not None
    h.session.add_usage(RunUsage(input_tokens=5, output_tokens=1))
    stats_base = default_stats_base(sessions)
    # dual files under stats_base
    found = sorted(stats_base.rglob("turns.jsonl"))
    assert len(found) == 2  # workspace + global
    bodies = [p.read_text().strip() for p in found]
    assert all(bodies)
    assert bodies[0] == bodies[1]
    assert '"input_tokens":5' in bodies[0] or '"input_tokens": 5' in bodies[0]


def test_with_sessions_stats_false_writes_nothing(tmp_path: Path):
    sessions = tmp_path / "sessions"
    h = (HarnessBuilder(workspace=tmp_path / "ws", model=TestModel())
         .with_sessions(dir=sessions, stats=False).build())
    assert h.session.stats_recorder is None
    h.session.add_usage(RunUsage(input_tokens=5, output_tokens=1))
    stats_base = default_stats_base(sessions)
    assert list(stats_base.rglob("turns.jsonl")) == []


def test_bare_builder_no_stats_recorder(tmp_path: Path):
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).build()
    assert h.session.stats_recorder is None
    h.session.add_usage(RunUsage(input_tokens=1, output_tokens=1))
    # no default XDG pollution asserted here — bare build must not create a
    # ledger object; add_usage without recorder is a pure in-memory +=
