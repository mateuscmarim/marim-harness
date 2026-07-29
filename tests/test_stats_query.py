"""Pure stats aggregations — no filesystem."""
from __future__ import annotations

from datetime import date

from marim_harness.stats.query import models, overview
from marim_harness.stats.types import TurnEvent


def _e(
    day: str,
    *,
    session_id: str = "s1",
    model: str | None = "opus",
    inp: int = 100,
    out: int = 50,
    dur: float | None = 10.0,
    workspace: str = "ws-aaa",
) -> TurnEvent:
    return TurnEvent(
        v=1,
        ts=f"{day}T12:00:00+00:00",
        day=day,
        session_id=session_id,
        workspace=workspace,
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=None,
        cost_is_exact=False,
        session_duration_seconds=dur,
    )


def test_overview_empty():
    today = date(2026, 7, 28)
    o = overview([], "all", today=today)
    assert o.total_tokens == 0
    assert o.favorite_model is None
    assert o.sessions == 0
    assert o.longest_session is None
    assert o.active_days == 0
    assert o.window_days == 0
    assert o.most_active_day is None
    assert o.longest_streak == 0
    assert o.current_streak == 0
    assert len(o.heatmap) == 365
    assert all(h.tokens == 0 for h in o.heatmap)
    assert o.heatmap[0].day == "2025-07-29"  # today - 364
    assert o.heatmap[-1].day == "2026-07-28"


def test_overview_totals_and_favorite_tie_lex_smaller():
    today = date(2026, 7, 28)
    events = [
        _e("2026-07-28", model="sonnet", inp=100, out=0),
        _e("2026-07-28", model="opus", inp=100, out=0),
    ]
    o = overview(events, "all", today=today)
    assert o.total_tokens == 200
    # tie on tokens → lex smaller id wins
    assert o.favorite_model == "opus"
    assert o.sessions == 1


def test_most_active_day_tie_picks_latest():
    today = date(2026, 7, 28)
    events = [
        _e("2026-07-26", inp=100, out=0),
        _e("2026-07-27", inp=100, out=0),
    ]
    o = overview(events, "all", today=today)
    assert o.most_active_day == "2026-07-27"


def test_longest_and_current_streak():
    today = date(2026, 7, 28)
    # active: 25,26,27 and 28 — current streak 4; gap before 25
    events = [
        _e("2026-07-20"),
        _e("2026-07-25"),
        _e("2026-07-26"),
        _e("2026-07-27"),
        _e("2026-07-28"),
    ]
    o = overview(events, "all", today=today)
    assert o.longest_streak == 4
    assert o.current_streak == 4
    assert o.active_days == 5


def test_current_streak_uses_yesterday_when_today_inactive():
    today = date(2026, 7, 28)
    events = [_e("2026-07-26"), _e("2026-07-27")]
    o = overview(events, "all", today=today)
    assert o.current_streak == 2


def test_current_streak_ignores_7d_clip():
    """A 9-day streak must survive range='7d' for current_streak."""
    today = date(2026, 7, 28)
    days = [f"2026-07-{d:02d}" for d in range(20, 29)]  # 20..28 = 9 days
    events = [_e(d) for d in days]
    o = overview(events, "7d", today=today)
    assert o.current_streak == 9
    # summary active_days is range-clipped (7d window starts 2026-07-22)
    assert o.active_days == 7
    assert o.window_days == 7


def test_heatmap_ignores_range_filter():
    today = date(2026, 7, 28)
    events = [_e("2026-06-01", inp=500, out=0), _e("2026-07-28", inp=10, out=0)]
    o = overview(events, "7d", today=today)
    # June 1 is outside 7d summary but inside heatmap year
    by_day = {h.day: h.tokens for h in o.heatmap}
    assert by_day["2026-06-01"] == 500
    assert o.total_tokens == 10  # 7d summary only has July 28


def test_longest_session_per_session_max_snapshot():
    today = date(2026, 7, 28)
    events = [
        _e("2026-07-28", session_id="a", dur=100.0),
        _e("2026-07-28", session_id="a", dur=250.0),
        _e("2026-07-28", session_id="b", dur=200.0),
    ]
    o = overview(events, "all", today=today)
    assert o.longest_session == 250.0


def test_models_share_and_series():
    today = date(2026, 7, 28)
    events = [
        _e("2026-07-27", model="opus", inp=70, out=30),
        _e("2026-07-28", model="sonnet", inp=40, out=10),
        _e("2026-07-28", model="opus", inp=10, out=0),
    ]
    r = models(events, "all", today=today)
    assert r.totals[0].model == "opus"
    assert r.totals[0].total_tokens == 110
    assert r.totals[0].input_tokens == 80
    assert r.totals[0].output_tokens == 30
    assert abs(r.totals[0].share - 110 / 160) < 1e-9
    assert r.totals[1].model == "sonnet"
    assert abs(sum(t.share for t in r.totals) - 1.0) < 1e-9
    # series covers min day .. today
    assert r.series[0].day == "2026-07-27"
    assert r.series[-1].day == "2026-07-28"
    last = r.series[-1].by_model
    assert last == {"sonnet": 50, "opus": 10}


def test_models_series_spans_bounded_window():
    """A 7d report covers all 7 days, even though only today has data."""
    today = date(2026, 7, 28)
    r = models([_e("2026-07-28", inp=1, out=1)], "7d", today=today)
    assert len(r.series) == 7
    assert r.series[0].day == "2026-07-22"  # today - 6
    assert r.series[-1].day == "2026-07-28"
    assert [d.by_model for d in r.series[:-1]] == [{}] * 6
    assert r.series[-1].by_model == {"opus": 2}


def test_models_series_30d_window():
    today = date(2026, 7, 28)
    r = models([_e("2026-07-20")], "30d", today=today)
    assert len(r.series) == 30
    assert r.series[0].day == "2026-06-29"  # today - 29
    assert r.series[-1].day == "2026-07-28"
    assert r.series[21].by_model == {"opus": 150}


def test_models_series_empty_bounded_range_is_full_zero_window():
    today = date(2026, 7, 28)
    r = models([], "7d", today=today)
    assert len(r.series) == 7
    assert all(d.by_model == {} for d in r.series)
    assert r.totals == []


def test_models_series_empty_all_range_is_empty():
    """``all`` has no window start, so with no data there is nothing to span."""
    r = models([], "all", today=date(2026, 7, 28))
    assert r.series == []


def test_models_series_all_range_spans_min_day_through_today():
    today = date(2026, 7, 28)
    r = models([_e("2026-07-25")], "all", today=today)
    assert [d.day for d in r.series] == [
        "2026-07-25", "2026-07-26", "2026-07-27", "2026-07-28",
    ]


def test_models_series_extends_past_today_for_future_events():
    """Clock skew / hand-edited days must not make series and totals disagree."""
    today = date(2026, 7, 28)
    r = models([_e("2026-07-30", inp=3, out=4)], "7d", today=today)
    assert r.series[0].day == "2026-07-22"
    assert r.series[-1].day == "2026-07-30"
    assert len(r.series) == 9
    assert r.series[-1].by_model == {"opus": 7}
    # the event is in the totals, so it must be somewhere in the series too
    assert r.totals[0].total_tokens == 7


def test_models_series_absurd_future_day_is_bounded():
    """A single garbage day must not allocate millions of entries."""
    today = date(2026, 7, 28)
    r = models([_e("9999-01-01")], "7d", today=today)
    assert len(r.series) == 3660
    assert r.series[0].day == "2026-07-22"


def test_unknown_model_bucket():
    today = date(2026, 7, 28)
    o = overview([_e("2026-07-28", model=None)], "all", today=today)
    assert o.favorite_model == "unknown"
