"""Stats JSONL ledger I/O."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai.usage import RunUsage

from marim_harness.stats.ledger import (
    StatsLedger,
    default_stats_base,
    event_from_dict,
    iter_turns,
    load_models,
    load_overview,
    workspace_slug,
)
from marim_harness.stats.recorder import LedgerStatsRecorder, NullStatsRecorder
from marim_harness.stats.types import TurnEvent
from marim_harness.usage import COST_DETAIL_KEY


def _event(**over) -> TurnEvent:
    base = dict(
        v=1,
        ts="2026-07-28T12:00:00+00:00",
        day="2026-07-28",
        session_id="s1",
        workspace="proj-deadbeefcafe",
        model="opus",
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.01,
        cost_is_exact=True,
        session_duration_seconds=3.5,
    )
    base.update(over)
    return TurnEvent(**base)


def test_default_stats_base_sibling_of_sessions(tmp_path: Path):
    assert default_stats_base(tmp_path / "sessions") == tmp_path / "stats"
    assert default_stats_base(tmp_path / "my-sessions") == tmp_path / "my-sessions" / "stats"


def test_workspace_slug_stable(tmp_path: Path):
    root = tmp_path / "marim-harness"
    root.mkdir()
    a = workspace_slug(root)
    b = workspace_slug(root)
    assert a == b
    assert a.startswith("marim-harness-")
    assert len(a.rsplit("-", 1)[-1]) == 12


def test_append_dual_write(tmp_path: Path):
    slug = "ws-abc"
    ledger = StatsLedger(tmp_path, slug)
    ev = _event(workspace=slug)
    ledger.append(ev)
    assert ledger.workspace_path.exists()
    assert ledger.global_path.exists()
    w_lines = ledger.workspace_path.read_text().strip().splitlines()
    g_lines = ledger.global_path.read_text().strip().splitlines()
    assert len(w_lines) == 1 and w_lines == g_lines
    data = json.loads(w_lines[0])
    assert data["session_id"] == "s1"
    assert data["input_tokens"] == 10
    assert data["workspace"] == slug


def test_iter_skips_corrupt_and_unknown_v(tmp_path: Path):
    path = tmp_path / "turns.jsonl"
    good = _event()
    from marim_harness.stats.ledger import event_to_dict
    lines = [
        "not-json",
        json.dumps({**event_to_dict(good), "v": 99}),
        json.dumps(event_to_dict(good)),
        "",
    ]
    path.write_text("\n".join(lines) + "\n")
    got = list(iter_turns(path))
    assert len(got) == 1
    assert got[0].session_id == "s1"


def test_append_does_not_raise_on_readonly(tmp_path: Path, monkeypatch):
    ledger = StatsLedger(tmp_path / "nope", "ws")
    # Force open to fail
    def boom(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr("builtins.open", boom)
    ledger.append(_event())  # must not raise


def test_event_from_dict_defaults():
    ev = event_from_dict({
        "v": 1,
        "ts": "t",
        "day": "2026-07-28",
        "session_id": "s",
        "workspace": "w",
    })
    assert ev is not None
    assert ev.input_tokens == 0
    assert ev.model is None
    assert ev.cost_usd is None


def test_null_recorder_noop():
    NullStatsRecorder().record(RunUsage(input_tokens=1, output_tokens=1))


def test_ledger_recorder_skips_zero_and_writes_nonzero(tmp_path: Path):
    ledger = StatsLedger(tmp_path, "ws-x")
    rec = LedgerStatsRecorder(
        ledger,
        session_id="sess-1",
        get_model_id=lambda: "anthropic/claude-sonnet-4-6",
        get_duration_seconds=lambda: 42.0,
    )
    rec.record(RunUsage())  # zero
    assert not ledger.workspace_path.exists()
    rec.record(RunUsage(
        input_tokens=100, output_tokens=20,
        cache_read_tokens=10, cache_write_tokens=5,
        details={COST_DETAIL_KEY: 1_000_000},  # $1.00 exact
    ))
    events = list(ledger.iter_workspace())
    assert len(events) == 1
    e = events[0]
    assert e.session_id == "sess-1"
    assert e.model == "anthropic/claude-sonnet-4-6"
    assert e.input_tokens == 100
    assert e.output_tokens == 20
    assert e.cache_read_tokens == 10
    assert e.session_duration_seconds == 42.0
    assert e.cost_usd == 1.0
    assert e.cost_is_exact is True
    assert e.day  # non-empty YYYY-MM-DD


def test_load_overview_workspace_and_global(tmp_path: Path):
    from datetime import date

    from marim_harness.stats.ledger import event_to_dict

    def write(path: Path, *events: TurnEvent):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(event_to_dict(e)) + "\n" for e in events))

    e1 = _event(workspace="ws-a", session_id="s1", input_tokens=50, output_tokens=0)
    e2 = _event(workspace="ws-b", session_id="s2", input_tokens=70, output_tokens=0)
    write(tmp_path / "ws-a" / "turns.jsonl", e1)
    write(tmp_path / "global" / "turns.jsonl", e1, e2)
    today = date(2026, 7, 28)
    ow = load_overview("workspace", "all", stats_base=tmp_path, workspace_slug="ws-a", today=today)
    assert ow.total_tokens == 50
    assert ow.sessions == 1
    og = load_overview("global", "all", stats_base=tmp_path, today=today)
    assert og.total_tokens == 120
    assert og.sessions == 2


def test_load_overview_workspace_requires_slug(tmp_path: Path):
    with pytest.raises(ValueError):
        load_overview("workspace", "all", stats_base=tmp_path)


def test_load_models_workspace_and_global(tmp_path: Path):
    from datetime import date

    from marim_harness.stats.ledger import event_to_dict

    def write(path: Path, *events: TurnEvent):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(event_to_dict(e)) + "\n" for e in events))

    e1 = _event(workspace="ws-a", session_id="s1", model="opus", input_tokens=50, output_tokens=0)
    e2 = _event(workspace="ws-b", session_id="s2", model="haiku", input_tokens=70, output_tokens=0)
    write(tmp_path / "ws-a" / "turns.jsonl", e1)
    write(tmp_path / "global" / "turns.jsonl", e1, e2)
    today = date(2026, 7, 28)
    mw = load_models("workspace", "all", stats_base=tmp_path, workspace_slug="ws-a", today=today)
    assert [t.model for t in mw.totals] == ["opus"]
    mg = load_models("global", "all", stats_base=tmp_path, today=today)
    assert {t.model for t in mg.totals} == {"opus", "haiku"}
