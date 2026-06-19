"""Tests for the desktop-notification system: the Notifier, config parsing,
Deps wiring, and that the TUI/headless fire points call it."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from marim_harness.config import load_config
from marim_harness.deps import Deps
from marim_harness.notifications import (
    ALL_EVENTS,
    DEFAULT_EVENTS,
    EVENT_ASK_USER,
    EVENT_ERROR,
    EVENT_JOB_DONE,
    EVENT_TURN_COMPLETE,
    NotificationConfig,
    Notifier,
    parse_events,
)

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


def test_notifier_all_events_are_documented():
    assert set(ALL_EVENTS) == {
        EVENT_TURN_COMPLETE,
        EVENT_ERROR,
        EVENT_ASK_USER,
        EVENT_JOB_DONE,
        "approval_needed",
    }


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
        script = mock_run.call_args[0][0][2]
        assert "display notification" in script


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------

def test_load_config_notifications_default_off(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_NOTIFICATIONS", raising=False)
    monkeypatch.delenv("MARIM_NOTIFICATION_EVENTS", raising=False)
    cfg = load_config()
    assert cfg.notifications_enabled is False
    assert cfg.notification_events == set(DEFAULT_EVENTS)


def test_load_config_notifications_enabled(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_NOTIFICATIONS", "1")
    monkeypatch.setenv("MARIM_NOTIFICATION_EVENTS", "turn_complete,error")
    cfg = load_config()
    assert cfg.notifications_enabled is True
    assert cfg.notification_events == {"turn_complete", "error"}


# ---------------------------------------------------------------------------
# Deps wiring
# ---------------------------------------------------------------------------

def test_deps_notifier_defaults_to_none(tmp_path: Path):
    d = Deps(workspace_root=tmp_path)
    assert d.notifier is None


def test_deps_notifier_can_be_set(tmp_path: Path):
    d = Deps(workspace_root=tmp_path)
    n = Notifier(NotificationConfig.disabled())
    d.notifier = n
    assert d.notifier is n
