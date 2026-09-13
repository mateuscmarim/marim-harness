"""The CLI backends' context report (``config/context_report.py``) and the
quota value objects / parsers it shipped with (``config/quota.py``,
``claude/quota.py``). Pure helpers, exercised directly."""

from __future__ import annotations

from types import SimpleNamespace

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from marim_harness.claude.quota import quota_from_usage
from marim_harness.config.context_report import (
    CONTEXT_REPORT_KEY,
    ContextReport,
    current_context_report,
    last_context_report,
    prompt_tokens,
)
from marim_harness.config.quota import QuotaHint, QuotaWindow

# --- ContextReport ----------------------------------------------------------------


def test_percent_needs_a_window():
    assert ContextReport(45_000, 200_000).percent == 22
    assert ContextReport(45_000).percent is None
    assert ContextReport(0, 200_000).percent == 0


def test_with_window_keeps_a_known_window_when_given_none():
    r = ContextReport(10, 200_000)
    assert r.with_window(None) is r
    assert r.with_window(128_000) == ContextReport(10, 128_000)
    assert ContextReport(10).with_window(200_000).window == 200_000


def test_payload_round_trip_and_rejection():
    r = ContextReport(27_516, 200_000)
    assert ContextReport.from_payload(r.to_payload()) == r
    assert ContextReport.from_payload({"used": 5, "window": None}) == ContextReport(5)
    # Malformed: not a dict, a bool/negative/missing `used`, a junk window.
    assert ContextReport.from_payload(None) is None
    assert ContextReport.from_payload("x") is None
    assert ContextReport.from_payload({"window": 1}) is None
    assert ContextReport.from_payload({"used": True}) is None
    assert ContextReport.from_payload({"used": -1}) is None
    assert ContextReport.from_payload({"used": 1, "window": "big"}) == ContextReport(1)
    assert ContextReport.from_payload({"used": 1, "window": 0}) == ContextReport(1)


def test_prompt_tokens_folds_both_cache_buckets():
    assert prompt_tokens(None) == 0
    assert (
        prompt_tokens(
            {
                "input_tokens": 3,
                "cache_read_input_tokens": 27_000,
                "cache_creation_input_tokens": 500,
                "output_tokens": 999,
            }
        )
        == 27_503
    )
    # Best-effort: junk buckets count as zero, never raise.
    assert prompt_tokens({"input_tokens": "lots", "cache_read_input_tokens": None}) == 0
    assert prompt_tokens({"input_tokens": True, "cache_read_input_tokens": 5.0}) == 5
    assert prompt_tokens("not a dict") == 0  # type: ignore[arg-type]


# --- history fallback ---------------------------------------------------------------


def _history(*details, provider: str = "claude-cli"):
    """A request/response pair per entry; each response carries ``details``
    as its provider_details (None for none) and names ``provider``."""
    out: list = []
    for d in details:
        out.append(ModelRequest(parts=[UserPromptPart(content="q")]))
        out.append(
            ModelResponse(parts=[TextPart(content="a")], provider_details=d, provider_name=provider)
        )
    return out


def test_last_context_report_reads_only_the_newest_response():
    payload = {CONTEXT_REPORT_KEY: {"used": 40_000, "window": 200_000}}
    assert last_context_report(_history(None, payload)) == ContextReport(40_000, 200_000)
    # An older report does not leak past a newer response without one (a
    # provider switch, or a pre-feature turn).
    assert last_context_report(_history(payload, None)) is None
    assert last_context_report(_history(payload, {"other": 1})) is None
    assert last_context_report([]) is None
    assert last_context_report([ModelRequest(parts=[UserPromptPart(content="q")])]) is None
    # With a provider named, a newest response from another one is not a
    # reading of THIS backend's context (an older same-provider one is not
    # consulted either — it is stale).
    assert last_context_report(_history(payload), "claude-cli") == ContextReport(40_000, 200_000)
    assert last_context_report(_history(payload), "codex-cli") is None
    assert last_context_report(_history(payload, None, provider="codex-cli"), "codex-cli") is None


def test_current_context_report_prefers_live_then_persisted_then_nothing():
    payload = {CONTEXT_REPORT_KEY: {"used": 40_000, "window": 200_000}}
    history = _history(payload)
    # A model that never reports (marim's own providers): None even with a
    # persisted report in the history.
    assert current_context_report(SimpleNamespace(), history) is None
    # A CLI model before its first turn: the persisted reading.
    cold = SimpleNamespace(context_report=None)
    assert current_context_report(cold, history) == ContextReport(40_000, 200_000)
    assert current_context_report(cold, []) is None
    # A live reading wins.
    live = SimpleNamespace(context_report=ContextReport(41_000, 200_000))
    assert current_context_report(live, history) == ContextReport(41_000, 200_000)
    # After a backend switch the persisted report belongs to the OTHER
    # backend: a cold codex model shows nothing until its own first turn.
    cold_codex = SimpleNamespace(context_report=None, provider_name="codex-cli")
    assert current_context_report(cold_codex, history) is None
    cold_claude = SimpleNamespace(context_report=None, provider_name="claude-cli")
    assert current_context_report(cold_claude, history) == ContextReport(40_000, 200_000)


# --- quota --------------------------------------------------------------------------


def test_quota_value_objects_render_like_codex():
    hint = QuotaHint(QuotaWindow(11, 300), QuotaWindow(59, 10080))
    assert hint.render() == "quota 11% (5h) · 59% (1w)"
    assert QuotaHint(None, None).render() == ""


def test_claude_quota_from_get_usage():
    # The live 2.1.270 shape (Max plan), trimmed to what the parser reads.
    payload = {
        "subscription_type": "max",
        "rate_limits_available": True,
        "rate_limits": {
            "five_hour": {"utilization": 11, "resets_at": "2026-09-13T22:40:00+00:00"},
            "seven_day": {"utilization": 59.4, "resets_at": "2026-09-16T18:00:00+00:00"},
            "seven_day_opus": None,
        },
    }
    hint = quota_from_usage(payload)
    assert hint == QuotaHint(QuotaWindow(11, 300), QuotaWindow(59, 10080))
    assert hint is not None and hint.render() == "quota 11% (5h) · 59% (1w)"


def test_claude_quota_is_none_without_windows():
    assert quota_from_usage(None) is None
    assert quota_from_usage({"rate_limits_available": False, "rate_limits": {}}) is None
    assert quota_from_usage({"rate_limits": {"five_hour": None, "seven_day": None}}) is None
    assert quota_from_usage({"rate_limits": {"five_hour": {"utilization": True}}}) is None
    # One window is enough.
    only = quota_from_usage({"rate_limits": {"seven_day": {"utilization": 3}}})
    assert only == QuotaHint(None, QuotaWindow(3, 10080))
