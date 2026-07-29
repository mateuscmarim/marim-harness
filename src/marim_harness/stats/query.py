"""Pure stats aggregations over :class:`TurnEvent` — no filesystem, no I/O.

Day boundaries are UTC. Token totals are ``input_tokens + output_tokens``;
cache tokens are a subset of input tokens and are never added again.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone

from .types import (
    DayModelSeries,
    HeatmapDay,
    ModelsReport,
    ModelTotal,
    Overview,
    Range,
    TurnEvent,
)

_HEATMAP_DAYS = 365


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def _event_tokens(e: TurnEvent) -> int:
    return e.input_tokens + e.output_tokens


def _model_key(e: TurnEvent) -> str:
    return e.model or "unknown"


def _range_start(range: Range, today: date) -> date | None:
    if range == "7d":
        return today - timedelta(days=6)
    if range == "30d":
        return today - timedelta(days=29)
    return None


def _filter_range(events: Iterable[TurnEvent], range: Range, today: date) -> list[TurnEvent]:
    start = _range_start(range, today)
    if start is None:
        return list(events)
    return [e for e in events if _parse_day(e.day) >= start]


def _active_days(events: Iterable[TurnEvent]) -> set[date]:
    return {_parse_day(e.day) for e in events}


def _streak_ending(active: set[date], end: date) -> int:
    count = 0
    d = end
    while d in active:
        count += 1
        d -= timedelta(days=1)
    return count


def _run_length_from(active: set[date], start: date) -> int:
    count = 0
    d = start
    while d in active:
        count += 1
        d += timedelta(days=1)
    return count


def _longest_streak(active: set[date]) -> int:
    longest = 0
    for d in active:
        if (d - timedelta(days=1)) in active:
            continue  # not the start of a run
        longest = max(longest, _run_length_from(active, d))
    return longest


def _current_streak(active: set[date], today: date) -> int:
    if today in active:
        return _streak_ending(active, today)
    yesterday = today - timedelta(days=1)
    if yesterday in active:
        return _streak_ending(active, yesterday)
    return 0


def _heatmap(events: Iterable[TurnEvent], today: date) -> list[HeatmapDay]:
    start = today - timedelta(days=_HEATMAP_DAYS - 1)
    totals: dict[date, int] = defaultdict(int)
    for e in events:
        d = _parse_day(e.day)
        if start <= d <= today:
            totals[d] += _event_tokens(e)
    days = [start + timedelta(days=i) for i in range(_HEATMAP_DAYS)]
    return [HeatmapDay(day=d.isoformat(), tokens=totals[d]) for d in days]


def _favorite_model(model_totals: dict[str, int]) -> str | None:
    if not model_totals:
        return None
    return min(model_totals, key=lambda m: (-model_totals[m], m))


def _most_active_day(day_totals: dict[date, int]) -> str | None:
    if not day_totals:
        return None
    best = min(day_totals, key=lambda d: (-day_totals[d], -d.toordinal()))
    return best.isoformat()


def _longest_session(events: Iterable[TurnEvent]) -> float | None:
    session_max: dict[str, float] = {}
    for e in events:
        if e.session_duration_seconds is None:
            continue
        prev = session_max.get(e.session_id)
        if prev is None or e.session_duration_seconds > prev:
            session_max[e.session_id] = e.session_duration_seconds
    return max(session_max.values()) if session_max else None

def _window_days(range: Range, active_ranged: set[date]) -> int:
    if range == "7d":
        return 7
    if range == "30d":
        return 30
    if not active_ranged:
        return 0
    return (max(active_ranged) - min(active_ranged)).days + 1


def overview(events: Iterable[TurnEvent], range: Range, *, today: date | None = None) -> Overview:
    today = today if today is not None else _utc_today()
    all_events = list(events)
    ranged = _filter_range(all_events, range, today)

    model_totals: dict[str, int] = defaultdict(int)
    day_totals: dict[date, int] = defaultdict(int)
    for e in ranged:
        model_totals[_model_key(e)] += _event_tokens(e)
        day_totals[_parse_day(e.day)] += _event_tokens(e)

    active_ranged = _active_days(ranged)
    active_all = _active_days(all_events)

    return Overview(
        total_tokens=sum(_event_tokens(e) for e in ranged),
        favorite_model=_favorite_model(model_totals),
        sessions=len({e.session_id for e in ranged}),
        longest_session=_longest_session(ranged),
        active_days=len(active_ranged),
        window_days=_window_days(range, active_ranged),
        most_active_day=_most_active_day(day_totals),
        longest_streak=_longest_streak(active_ranged),
        current_streak=_current_streak(active_all, today),
        heatmap=_heatmap(all_events, today),
    )


def _model_totals(ranged: list[TurnEvent]) -> list[ModelTotal]:
    bucket: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e in ranged:
        pair = bucket[_model_key(e)]
        pair[0] += e.input_tokens
        pair[1] += e.output_tokens
    grand_total = sum(i + o for i, o in bucket.values())
    totals = [
        ModelTotal(
            model=m,
            input_tokens=i,
            output_tokens=o,
            total_tokens=i + o,
            share=(i + o) / grand_total if grand_total else 0.0,
        )
        for m, (i, o) in bucket.items()
    ]
    totals.sort(key=lambda t: (-t.total_tokens, t.model))
    return totals


def _day_model_series(ranged: list[TurnEvent], today: date) -> list[DayModelSeries]:
    if not ranged:
        return []
    day_model_totals: dict[date, dict[str, int]] = defaultdict(dict)
    for e in ranged:
        d = _parse_day(e.day)
        bucket = day_model_totals[d]
        key = _model_key(e)
        bucket[key] = bucket.get(key, 0) + _event_tokens(e)

    min_day = min(day_model_totals)
    span = (today - min_day).days + 1
    return [
        DayModelSeries(
            day=(min_day + timedelta(days=i)).isoformat(),
            by_model=dict(day_model_totals.get(min_day + timedelta(days=i), {})),
        )
        for i in range(span)
    ]


def models(events: Iterable[TurnEvent], range: Range, *, today: date | None = None) -> ModelsReport:
    today = today if today is not None else _utc_today()
    ranged = _filter_range(list(events), range, today)
    return ModelsReport(
        series=_day_model_series(ranged, today),
        totals=_model_totals(ranged),
    )
