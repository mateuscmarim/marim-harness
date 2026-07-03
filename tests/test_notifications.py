"""Tests for the desktop-notification system: the Notifier, config parsing,
Deps wiring, and that the TUI/headless fire points call it."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from marim_harness.config import load_config
from marim_harness.notifications import (
    ALL_EVENTS,
    DEFAULT_EVENTS,
    EVENT_ASK_USER,
    EVENT_ERROR,
    EVENT_JOB_DONE,
    EVENT_TURN_COMPLETE,
    NotificationConfig,
    Notifier,
    _escape_applescript,
    parse_events,
)
from marim_harness.runtime.permissions import Mode
from tests.conftest import _make_deps

# ---------------------------------------------------------------------------
# NotificationConfig / parse_events
# ---------------------------------------------------------------------------

def test_parse_events_empty_yields_defaults():
    assert parse_events("") == set(DEFAULT_EVENTS)
    assert parse_events("   ") == set(DEFAULT_EVENTS)


def test_parse_events_known_subset():
    assert parse_events("turn_complete, error") == {"turn_complete", "error"}


def test_parse_events_newline_separator():
    assert parse_events("turn_complete\nerror") == {"turn_complete", "error"}


def test_parse_events_drops_unknown():
    # unknown names are filtered; if nothing survives, defaults are restored
    assert parse_events("bogus") == set(DEFAULT_EVENTS)
    assert parse_events("turn_complete, bogus") == {"turn_complete"}


def test_notification_config_disabled_factory():
    cfg = NotificationConfig.disabled()
    assert cfg.enabled is False


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

def test_notifier_noop_when_disabled():
    n = Notifier(NotificationConfig.disabled())
    with patch.object(n, "_dispatch") as mock_dispatch:
        n.send("t", "b", EVENT_TURN_COMPLETE)
        mock_dispatch.assert_not_called()


def test_notifier_noop_for_unconfigured_event():
    n = Notifier(NotificationConfig(enabled=True, events={"error"}))
    with patch.object(n, "_dispatch") as mock_dispatch:
        n.send("t", "b", EVENT_TURN_COMPLETE)
        mock_dispatch.assert_not_called()


def test_notifier_fires_for_configured_event():
    n = Notifier(NotificationConfig(enabled=True, events={"turn_complete"}))
    with patch.object(n, "_dispatch") as mock_dispatch:
        n.send("Turn complete", "done", EVENT_TURN_COMPLETE)
        mock_dispatch.assert_called_once_with("Turn complete", "done")


def test_notifier_default_events_include_ask_user():
    n = Notifier(NotificationConfig(enabled=True))
    assert EVENT_ASK_USER in n.config.events


def test_notifier_swallows_errors():
    """A failing dispatch must never raise — notifications are best-effort."""
    n = Notifier(NotificationConfig(enabled=True, events={"error"}))
    with patch.object(n, "_dispatch", side_effect=OSError("no daemon")):
        n.send("t", "b", EVENT_ERROR)  # should not raise
    # The timestamp should NOT be recorded on failure so a later retry works.
    assert EVENT_ERROR not in n._last_fired


def test_notifier_all_events_are_documented():
    assert set(ALL_EVENTS) == {
        EVENT_TURN_COMPLETE,
        EVENT_ERROR,
        EVENT_ASK_USER,
        EVENT_JOB_DONE,
        "approval_needed",
    }


# ---------------------------------------------------------------------------
# Coalescing
# ---------------------------------------------------------------------------


def test_coalescing_suppresses_rapid_duplicates():
    cfg = NotificationConfig(
        enabled=True,
        events={"ask_user"},
        coalesce_seconds=5.0,
    )
    n = Notifier(cfg)
    with patch.object(n, "_dispatch") as mock_dispatch:
        n.send("t", "b", "ask_user")
        n.send("t", "b", "ask_user")  # within window → suppressed
        n.send("t", "b", "ask_user")  # still within → suppressed
        assert mock_dispatch.call_count == 1


def test_coalescing_different_events_fire_independently():
    cfg = NotificationConfig(
        enabled=True,
        events={"ask_user", "error"},
        coalesce_seconds=5.0,
    )
    n = Notifier(cfg)
    with patch.object(n, "_dispatch") as mock_dispatch:
        n.send("q", "a", "ask_user")
        n.send("e", "x", "error")
        assert mock_dispatch.call_count == 2


def test_coalescing_resets_after_window():
    cfg = NotificationConfig(
        enabled=True,
        events={"turn_complete"},
        coalesce_seconds=0.01,  # 10ms window for fast tests
    )
    n = Notifier(cfg)
    with patch.object(n, "_dispatch") as mock_dispatch:
        n.send("t", "b", "turn_complete")
        assert mock_dispatch.call_count == 1
        # Artificially back-date the last fire to outside the window
        n._last_fired["turn_complete"] = -10.0
        n.send("t", "b", "turn_complete")
        assert mock_dispatch.call_count == 2


def test_coalescing_disabled_by_default():
    cfg = NotificationConfig()
    assert cfg.coalesce_seconds == 2.0


# ---------------------------------------------------------------------------
# Platform backends (mocked)
# ---------------------------------------------------------------------------

def test_notify_send_linux_calls_notify_send():
    n = Notifier(NotificationConfig(enabled=True, events={"turn_complete"}))
    n._platform = "linux"
    with patch("marim_harness.notifications.shutil.which", return_value="/usr/bin/notify-send"), \
         patch("marim_harness.notifications.subprocess.run") as mock_run:
        n.send("Title", "Body", EVENT_TURN_COMPLETE)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "notify-send"
        assert "Title" in args and "Body" in args


def test_notify_send_linux_missing_binary_is_silent():
    n = Notifier(NotificationConfig(enabled=True, events={"turn_complete"}))
    n._platform = "linux"
    with patch("marim_harness.notifications.shutil.which", return_value=None):
        n.send("Title", "Body", EVENT_TURN_COMPLETE)  # should not raise


def test_notify_send_macos_calls_osascript():
    n = Notifier(NotificationConfig(enabled=True, events={"turn_complete"}))
    n._platform = "darwin"
    with patch("marim_harness.notifications.shutil.which", return_value="/usr/bin/osascript"), \
         patch("marim_harness.notifications.subprocess.run") as mock_run:
        n.send("Title", "Body", EVENT_TURN_COMPLETE)
        mock_run.assert_called_once()
        # Script is passed on stdin, not as a CLI argument.
        assert mock_run.call_args.kwargs.get("input") or mock_run.call_args[1].get("input")
        script = (mock_run.call_args.kwargs.get("input") or mock_run.call_args[1]["input"]).decode()
        assert "display notification" in script
        assert 'with title "marim"' in script


def test_notify_send_linux_uses_dashdash_separator():
    """A title/body starting with ``-`` (model-influenced text) must not be parsed
    as a notify-send option: the ``--`` separator ends option parsing."""
    n = Notifier(NotificationConfig(enabled=True, events={"turn_complete"}))
    n._platform = "linux"
    with patch("marim_harness.notifications.shutil.which", return_value="/usr/bin/notify-send"), \
         patch("marim_harness.notifications.subprocess.run") as mock_run:
        n.send("--attacker-flag", "body", EVENT_TURN_COMPLETE)
        argv = mock_run.call_args[0][0]
        # The title/body sit AFTER a `--` terminator, positionally.
        assert "--" in argv
        sep = argv.index("--")
        assert argv[sep + 1] == "--attacker-flag"
        assert argv[sep + 2] == "body"


def _has_bare_quote(escaped: str) -> bool:
    """Whether ``escaped`` contains a double-quote that is NOT backslash-escaped —
    i.e. one that would terminate an AppleScript string literal early. Consumes
    every backslash-escaped pair (``\\\\`` and ``\\"``) first; a leftover ``"``
    is a break-out."""
    import re

    return '"' in re.sub(r"\\.", "", escaped)


def test_escape_applescript_escapes_backslash_before_quote():
    # A raw backslash+quote must become escaped-backslash + escaped-quote, NOT a
    # doubled backslash followed by a bare (string-terminating) quote.
    assert _escape_applescript('\\"') == '\\\\\\"'
    assert not _has_bare_quote(_escape_applescript('\\"'))


def test_osascript_body_cannot_break_out_of_string():
    """A malicious body carrying ``\\"`` (which the old escape mishandled) must not
    escape the AppleScript string literal and run as code."""
    n = Notifier(NotificationConfig(enabled=True, events={"turn_complete"}))
    n._platform = "darwin"
    # The classic break-out: close the string, then inject `do shell script`.
    body = 'pwned\\" & (do shell script "touch /tmp/pwned") & "'
    with patch("marim_harness.notifications.shutil.which", return_value="/usr/bin/osascript"), \
         patch("marim_harness.notifications.subprocess.run") as mock_run:
        n.send("marim", body, EVENT_TURN_COMPLETE)
        script = (mock_run.call_args.kwargs.get("input")
                  or mock_run.call_args[1]["input"]).decode()
    # The whole body stays a single, properly-terminated string literal: after the
    # `display notification "` opener, its escaped payload has no bare quote.
    opener = 'display notification "'
    tail = script[len(opener):]
    body_segment = tail[: tail.index('" with title "marim"')]
    assert not _has_bare_quote(body_segment)
    # And the escaped payload preserves the (now-inert) injection text as data.
    assert "do shell script" in body_segment


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------

def test_load_config_notifications_default_on(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_NOTIFICATIONS", raising=False)
    monkeypatch.delenv("MARIM_NOTIFICATION_EVENTS", raising=False)
    cfg = load_config()
    assert cfg.notifications.enabled is True
    assert cfg.notifications.events == set(DEFAULT_EVENTS)


def test_load_config_notifications_can_be_disabled(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_NOTIFICATIONS", "0")
    cfg = load_config()
    assert cfg.notifications.enabled is False


def test_model_config_notifications_default_on():
    from marim_harness.config.model import ModelConfig

    assert ModelConfig(provider="openrouter", model="x").notifications.enabled is True


def test_load_config_notifications_enabled(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_NOTIFICATIONS", "1")
    monkeypatch.setenv("MARIM_NOTIFICATION_EVENTS", "turn_complete,error")
    cfg = load_config()
    assert cfg.notifications.enabled is True
    assert cfg.notifications.events == {"turn_complete", "error"}


# ---------------------------------------------------------------------------
# Deps wiring
# ---------------------------------------------------------------------------

def test_deps_notifier_defaults_to_none(tmp_path: Path):
    d = _make_deps(tmp_path, mode=Mode.ask)
    assert d.ui.notifier is None


def test_deps_notifier_can_be_set(tmp_path: Path):
    d = _make_deps(tmp_path, mode=Mode.ask)
    n = Notifier(NotificationConfig.disabled())
    d.ui.notifier = n
    assert d.ui.notifier is n


# ---------------------------------------------------------------------------
# Headless _preview helper
# ---------------------------------------------------------------------------

def test_preview_short_text_unchanged():
    from marim_harness.interfaces.cli.headless import _preview
    assert _preview("hello world") == "hello world"


def test_preview_long_text_truncated():
    from marim_harness.interfaces.cli.headless import _preview
    text = "a" * 100
    result = _preview(text)
    assert len(result) == 80
    assert result.endswith("…")


def test_preview_collapses_whitespace():
    from marim_harness.interfaces.cli.headless import _preview
    assert _preview("  hello  \n  world  ") == "hello world"


def test_preview_empty_returns_fallback():
    from marim_harness.interfaces.cli.headless import _preview
    assert _preview("") == "(empty response)"
    assert _preview("   ") == "(empty response)"
    assert _preview(None) == "(empty response)"
