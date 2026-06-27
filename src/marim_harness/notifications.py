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

import asyncio
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

    def _should_fire(self, event_type: str) -> bool:
        """Shared gate for the sync and async send paths: enabled, the event is
        configured, and we're outside this event type's coalesce window. Records
        the fire timestamp as a side effect so callers don't double-fire — only
        call this once per intended notification."""
        if not self.config.enabled:
            return False
        if event_type not in self.config.events:
            return False
        now = time.monotonic()
        last = self._last_fired.get(event_type)
        if last is not None and (now - last) < self.config.coalesce_seconds:
            return False
        self._last_fired[event_type] = now
        return True

    def send(self, title: str, body: str, event_type: str) -> None:
        """Fire a notification for ``event_type`` if enabled and configured.

        ``title`` and ``body`` are the notification's title/body text. Any
        error from the underlying OS call is caught and logged at debug —
        notifications are purely cosmetic and must never interrupt the agent.

        **Coalescing:** if the same ``event_type`` was dispatched less than
        ``coalesce_seconds`` ago, the call is silently skipped. This prevents
        a burst of duplicate notifications (e.g. rapid ask_user calls).

        This is the *blocking* path — it shells out with ``subprocess.run`` and
        waits. It is fine for the headless CLI, but the TUI must never call it on
        the event loop (the Windows backend alone sleeps ~5.5s); the TUI uses
        :meth:`send_async` instead, which runs the same dispatch off the loop.
        """
        if not self._should_fire(event_type):
            return
        try:
            self._dispatch(title, body)
        except Exception as exc:  # never let notifications break a turn
            # _should_fire already recorded the timestamp on a fire; roll it back
            # so a later retry isn't suppressed.
            self._last_fired.pop(event_type, None)
            logger.debug("notification failed (%s): %s", event_type, exc)

    async def send_async(self, title: str, body: str, event_type: str) -> None:
        """Non-blocking counterpart to :meth:`send` for the event loop. Spawns the
        platform notifier via ``asyncio.create_subprocess_exec`` and awaits it
        without blocking other tasks. Failures are swallowed like ``send``."""
        if not self._should_fire(event_type):
            return
        spec = self._command_for(title, body)
        if spec is None:
            return  # unknown platform or missing binary — nothing to run
        argv, stdin = spec
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate(stdin)
        except Exception as exc:  # never let notifications break a turn
            self._last_fired.pop(event_type, None)
            logger.debug("async notification failed (%s): %s", event_type, exc)

    def _dispatch(self, title: str, body: str) -> None:
        spec = self._command_for(title, body)
        if spec is None:
            return  # unknown platform or missing binary
        argv, stdin = spec
        subprocess.run(
            argv,
            input=stdin,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _command_for(self, title: str, body: str) -> tuple[list[str], bytes | None] | None:
        """Build the ``(argv, stdin_bytes)`` for the current platform's notifier,
        or None when there's nothing to run (unknown platform / missing binary).
        Single source of truth so the sync and async dispatch paths stay in step."""
        if self._platform.startswith("linux"):
            return self._notify_send_cmd(title, body)
        if self._platform == "darwin":
            return self._osascript_cmd(title, body)
        if self._platform == "win32":
            return self._powershell_cmd(title, body)
        return None  # Unknown platform: silently do nothing.

    # -- platform backends ------------------------------------------------

    @staticmethod
    def _notify_send_cmd(title: str, body: str) -> tuple[list[str], bytes | None] | None:
        if shutil.which("notify-send") is None:
            return None
        return ["notify-send", "--app-name=marim", title, body], None

    @staticmethod
    def _osascript_cmd(title: str, body: str) -> tuple[list[str], bytes | None] | None:
        if shutil.which("osascript") is None:
            return None
        # Pass the script on stdin to avoid shell interpretation entirely —
        # no escaping of title/body needed since they never touch a shell.
        script = (
            'display notification "' + body.replace('"', '\\"') + '" '
            'with title "marim" subtitle "' + title.replace('"', '\\"') + '"'
        )
        return ["osascript", "-"], script.encode()

    @staticmethod
    def _powershell_cmd(title: str, body: str) -> tuple[list[str], bytes | None] | None:
        # Use the BurntToast-free fallback: a balloon tip via the system tray.
        # This avoids any module install; works on Windows 10/11 PowerShell 5+.
        # Pass the script on stdin to avoid shell interpretation.
        t = title.replace("'", "''")
        b = body.replace("'", "''")
        ps = (
            "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms')"
            " | Out-Null;"
            "$n = New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon = [System.Drawing.SystemIcons]::Information;"
            f"$n.BalloonTipTitle = '{t}';"
            f"$n.BalloonTipText = '{b}';"
            "$n.Visible = $true;"
            "$n.ShowBalloonTip(5000);"
            "Start-Sleep -Milliseconds 5500;"
            "$n.Dispose()"
        )
        return ["powershell", "-NoProfile", "-Command", "-"], ps.encode()
