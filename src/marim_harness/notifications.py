"""Desktop notifications: a best-effort, cross-platform notifier that fires
native OS notifications for agent events (turn complete, error, approval
needed, ask user, background job finished).

Notifications are opt-in (``MARIM_NOTIFICATIONS=1``) and each event can be
individually toggled via ``MARIM_NOTIFICATION_EVENTS``. The notifier never
raises — a missing notification daemon or a failed ``subprocess`` call is
silently swallowed so it can never break the agent loop.

No third-party dependencies: Linux uses ``notify-send``, macOS uses
``osascript``, and Windows uses a PowerShell toast via ``subprocess``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Events the notifier knows about. The caller passes one of these to
# ``Notifier.send``; anything not in the configured set is a no-op.
EVENT_TURN_COMPLETE = "turn_complete"
EVENT_ERROR = "error"
EVENT_APPROVAL_NEEDED = "approval_needed"
EVENT_ASK_USER = "ask_user"
EVENT_JOB_DONE = "job_done"

ALL_EVENTS: tuple[str, ...] = (
    EVENT_TURN_COMPLETE,
    EVENT_ERROR,
    EVENT_APPROVAL_NEEDED,
    EVENT_ASK_USER,
    EVENT_JOB_DONE,
)

DEFAULT_EVENTS: tuple[str, ...] = (
    EVENT_TURN_COMPLETE,
    EVENT_ERROR,
    EVENT_APPROVAL_NEEDED,
    EVENT_ASK_USER,
)


def parse_events(raw: str) -> set[str]:
    """Parse a comma/newline-separated event list into a set, keeping only
    known event names. An empty string yields the default set."""
    if not raw or not raw.strip():
        return set(DEFAULT_EVENTS)
    parts = {p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()}
    return parts & set(ALL_EVENTS) or set(DEFAULT_EVENTS)


@dataclass
class NotificationConfig:
    """Whether and when desktop notifications fire."""

    enabled: bool = False
    events: set[str] = field(default_factory=lambda: set(DEFAULT_EVENTS))
    # Minimum seconds between two notifications of the same event type.
    # Rapid duplicate events (e.g. multiple ask_user calls) are suppressed.
    coalesce_seconds: float = 2.0

    @classmethod
    def disabled(cls) -> NotificationConfig:
        return cls(enabled=False, events=set())


class Notifier:
    """Sends native desktop notifications. Construct once (it lives on
    :class:`Deps`) and call :meth:`send` at each event fire point.

    Every call is a cheap guard: when disabled, or the event isn't in the
    configured set, or the platform's notification binary is missing, the call
    returns immediately with no work. Failures are logged at debug level and
    never raised.
    """

    def __init__(self, config: NotificationConfig | None = None) -> None:
        self.config = config or NotificationConfig.disabled()
        self._platform = sys.platform
        # Per-event-type monotonic timestamp of the last dispatched
        # notification. Used for coalescing rapid duplicates.
        self._last_fired: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def send(self, title: str, body: str, event_type: str) -> None:
        """Fire a notification for ``event_type`` if enabled and configured.

        ``title`` and ``body`` are the notification's title/body text. Any
        error from the underlying OS call is caught and logged at debug —
        notifications are purely cosmetic and must never interrupt the agent.

        **Coalescing:** if the same ``event_type`` was dispatched less than
        ``coalesce_seconds`` ago, the call is silently skipped. This prevents
        a burst of duplicate notifications (e.g. rapid ask_user calls).
        """
        if not self.config.enabled:
            return
        if event_type not in self.config.events:
            return
        now = time.monotonic()
        last = self._last_fired.get(event_type)
        if last is not None and (now - last) < self.config.coalesce_seconds:
            return
        try:
            self._dispatch(title, body)
            self._last_fired[event_type] = now
        except Exception as exc:  # never let notifications break a turn
            logger.debug("notification failed (%s): %s", event_type, exc)

    def _dispatch(self, title: str, body: str) -> None:
        if self._platform.startswith("linux"):
            self._notify_send(title, body)
        elif self._platform == "darwin":
            self._osascript(title, body)
        elif self._platform == "win32":
            self._powershell(title, body)
        # Unknown platform: silently do nothing.

    # -- platform backends ------------------------------------------------

    @staticmethod
    def _notify_send(title: str, body: str) -> None:
        if shutil.which("notify-send") is None:
            return
        subprocess.run(
            ["notify-send", "--app-name=marim", title, body],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _osascript(title: str, body: str) -> None:
        if shutil.which("osascript") is None:
            return
        # Pass the script on stdin to avoid shell interpretation entirely —
        # no escaping of title/body needed since they never touch a shell.
        script = (
            'display notification "' + body.replace('"', '\\"') + '" '
            'with title "marim" subtitle "' + title.replace('"', '\\"') + '"'
        )
        subprocess.run(
            ["osascript", "-"],
            input=script.encode(),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _powershell(title: str, body: str) -> None:
        # Use the BurntToast-free fallback: a balloon tip via the system tray.
        # This avoids any module install; works on Windows 10/11 PowerShell 5+.
        # Pass the script on stdin to avoid shell interpretation.
        ps = (
            "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms')"
            " | Out-Null;"
            "$n = New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon = [System.Drawing.SystemIcons]::Information;"
            f"$n.BalloonTipTitle = '{title.replace(chr(39), chr(39)*2)}';"
            f"$n.BalloonTipText = '{body.replace(chr(39), chr(39)*2)}';"
            "$n.Visible = $true;"
            "$n.ShowBalloonTip(5000);"
            "Start-Sleep -Milliseconds 5500;"
            "$n.Dispose()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "-"],
            input=ps.encode(),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
