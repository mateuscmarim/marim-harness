"""Lifecycle details round-trip independently of token/cost aggregation."""

from pathlib import Path
from types import SimpleNamespace

from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from marim_harness import HarnessBuilder
from marim_harness.stats.ledger import StatsLedger, event_from_dict, event_to_dict
from marim_harness.stats.recorder import LedgerStatsRecorder
from marim_harness.usage import COST_DETAIL_KEY


def test_backend_details_roundtrip_preserves_exact_spend(tmp_path: Path):
    ledger = StatsLedger(tmp_path, "ws")
    details = {
        "num_turns": 2,
        "duration_api_ms": 350,
        "stop_reason": "end_turn",
        "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t", "tool_input": "PRIVATE"}],
    }
    recorder = LedgerStatsRecorder(
        ledger,
        session_id="s",
        get_model_id=lambda: "claude-cli:x",
        get_duration_seconds=lambda: 1,
        get_backend_result=lambda: details,
    )
    recorder.record(
        RunUsage(input_tokens=100, output_tokens=20, details={COST_DETAIL_KEY: 250_000})
    )
    (event,) = ledger.iter_workspace()
    assert event.backend_result == {
        "num_turns": 2,
        "duration_api_ms": 350,
        "stop_reason": "end_turn",
        "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t"}],
    }
    assert (event.input_tokens, event.output_tokens, event.cost_usd) == (100, 20, 0.25)
    assert event.cost_is_exact
    old = event_to_dict(event)
    del old["backend_result"]
    restored = event_from_dict(old)
    assert restored is not None and restored.backend_result is None
    assert (restored.input_tokens, restored.output_tokens, restored.cost_usd) == (100, 20, 0.25)
    assert (
        event_from_dict(
            {**old, "backend_result": {"num_turns": True, "duration_api_ms": -1}}
        ).backend_result
        is None
    )
    assert "PRIVATE" not in ledger.workspace_path.read_text()


def test_optional_stats_failure_does_not_drop_billed_usage(tmp_path: Path, caplog):
    ledger = StatsLedger(tmp_path, "ws")

    def fail():
        raise ValueError("PRIVATE")

    recorder = LedgerStatsRecorder(
        ledger,
        session_id="s",
        get_model_id=lambda: "claude-cli:x",
        get_duration_seconds=lambda: 1,
        get_backend_result=fail,
    )
    recorder.record(RunUsage(input_tokens=20))
    (event,) = ledger.iter_workspace()
    assert event.input_tokens == 20 and event.backend_result is None
    assert "backend result stats refresh failed" in caplog.text
    assert "PRIVATE" not in caplog.text


def test_builder_wires_live_backend_details(tmp_path: Path):
    model = TestModel()
    model.lifecycle = SimpleNamespace(result_details={"num_turns": 3})
    harness = (
        HarnessBuilder(workspace=tmp_path / "ws", model=model)
        .with_sessions(dir=tmp_path / "sessions")
        .build()
    )
    harness.session.add_usage(RunUsage(input_tokens=5))
    (path,) = (tmp_path / "stats" / "global").glob("turns.jsonl")
    assert '"backend_result":{"num_turns":3}' in path.read_text()


def test_denied_zero_token_result_is_still_retained(tmp_path):
    ledger = StatsLedger(tmp_path, "ws")
    recorder = LedgerStatsRecorder(
        ledger,
        session_id="s",
        get_model_id=lambda: "claude-cli:x",
        get_duration_seconds=lambda: 1,
        get_backend_result=lambda: {"permission_denials": [{"tool_name": "Bash"}]},
    )
    recorder.record(RunUsage())
    (event,) = ledger.iter_workspace()
    assert event.backend_result == {"permission_denials": [{"tool_name": "Bash"}]}
    assert event.input_tokens == event.output_tokens == 0
