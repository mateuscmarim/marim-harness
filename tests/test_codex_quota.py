"""Pure parse/format helpers for the codex-cli quota hint."""

from __future__ import annotations

from marim_harness.codex.quota import QuotaHint, QuotaWindow, format_window, quota_from


def test_format_window_units():
    assert format_window(300) == "5h"
    assert format_window(10080) == "1w"
    assert format_window(2880) == "2d"
    assert format_window(90) == "90m"
    assert format_window(None) == ""
    assert format_window(0) == ""


def test_quota_from_snapshot_reads_both_windows():
    hint = quota_from(
        {
            "primary": {"usedPercent": 37, "resetsAt": 1, "windowDurationMins": 300},
            "secondary": {"usedPercent": 12, "windowDurationMins": 10080},
            "planType": "plus",
        }
    )
    assert hint == QuotaHint(QuotaWindow(37, 300), QuotaWindow(12, 10080))
    assert hint.render() == "quota 37% (5h) · 12% (1w)"


def test_quota_from_tolerates_missing_windows_and_garbage():
    assert quota_from(None) is None
    assert quota_from({}) is None
    assert quota_from({"primary": None, "secondary": {"resetsAt": 5}}) is None
    only = quota_from({"primary": {"usedPercent": 99, "windowDurationMins": "soon"}})
    assert only == QuotaHint(QuotaWindow(99, None), None)
    assert only.render() == "quota 99%"
    assert QuotaHint(None, None).render() == ""
