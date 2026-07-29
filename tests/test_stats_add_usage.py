from __future__ import annotations

from pathlib import Path

from pydantic_ai.usage import RunUsage

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.session.ctrl import SessionController


class _FakeRecorder:
    def __init__(self) -> None:
        self.seen: list[RunUsage] = []

    def record(self, delta: RunUsage) -> None:
        self.seen.append(delta)


def _session(tmp_path: Path, rec=None) -> SessionController:
    # Mirror the lightweight construction used across session unit tests:
    # Deps only needs WorkspaceConfig(root=...); other fields default.
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path))
    return SessionController(
        store=None, manager=None, deps=deps,
        max_context_tokens=100_000, keep_last_messages=10,
        stats_recorder=rec,
    )


def test_add_usage_banks_and_records(tmp_path: Path):
    rec = _FakeRecorder()
    s = _session(tmp_path, rec)
    s.add_usage(RunUsage(input_tokens=3, output_tokens=4))
    assert s.usage.input_tokens == 3
    assert s.usage.output_tokens == 4
    assert len(rec.seen) == 1
    assert rec.seen[0].input_tokens == 3


def test_add_usage_without_recorder(tmp_path: Path):
    s = _session(tmp_path, None)
    s.add_usage(RunUsage(input_tokens=1, output_tokens=2))
    assert s.usage.total_tokens == 3


def test_add_usage_recorder_error_does_not_raise(tmp_path: Path):
    class Boom:
        def record(self, delta):
            raise RuntimeError("boom")

    s = _session(tmp_path, Boom())
    s.add_usage(RunUsage(input_tokens=1, output_tokens=0))  # must not raise
    assert s.usage.input_tokens == 1


def test_duration_snapshot_includes_open_segment(tmp_path: Path, monkeypatch):
    s = _session(tmp_path)
    s.duration_seconds = 10.0
    s._segment_start = 100.0
    monkeypatch.setattr("marim_harness.session.ctrl.time.monotonic", lambda: 105.0)
    assert s.duration_snapshot() == 15.0
