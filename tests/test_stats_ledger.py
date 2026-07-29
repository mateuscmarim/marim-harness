"""Stats JSONL ledger I/O."""
from __future__ import annotations

import json
from pathlib import Path

from marim_harness.stats.ledger import (
    StatsLedger,
    default_stats_base,
    event_from_dict,
    iter_turns,
    workspace_slug,
)
from marim_harness.stats.types import TurnEvent


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
