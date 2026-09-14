"""Types for the durable usage ledger and its pure stats queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Range = Literal["all", "7d", "30d"]


@dataclass(frozen=True)
class TurnEvent:
    v: int
    ts: str
    day: str
    session_id: str
    workspace: str
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float | None
    cost_is_exact: bool
    session_duration_seconds: float | None
    backend_result: dict | None = None


@dataclass(frozen=True)
class HeatmapDay:
    day: str
    tokens: int


@dataclass(frozen=True)
class Overview:
    total_tokens: int
    favorite_model: str | None
    sessions: int
    longest_session: float | None
    active_days: int
    window_days: int
    most_active_day: str | None
    longest_streak: int
    current_streak: int
    heatmap: list[HeatmapDay]


@dataclass(frozen=True)
class ModelTotal:
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    share: float


@dataclass(frozen=True)
class DayModelSeries:
    day: str
    by_model: dict[str, int]


@dataclass(frozen=True)
class ModelsReport:
    series: list[DayModelSeries]
    totals: list[ModelTotal]


def normalize_backend_result(value: object) -> dict | None:
    """Optional display details, never token or cost inputs (including on replay)."""
    if not isinstance(value, dict):
        return None
    result: dict = {}
    for key in ("num_turns", "duration_api_ms"):
        count = value.get(key)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            result[key] = count
    if isinstance(value.get("stop_reason"), str):
        result["stop_reason"] = value["stop_reason"]
    denials = value.get("permission_denials")
    if isinstance(denials, list):
        result["permission_denials"] = [
            {key: row[key] for key in ("tool_name", "tool_use_id") if isinstance(row.get(key), str)}
            for row in denials
            if isinstance(row, dict)
        ]
    return result or None
