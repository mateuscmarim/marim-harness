"""Capture and format LSP diagnostics.

LSP diagnostics are *pushed* by the server (``textDocument/publishDiagnostics``)
after a file opens or changes — they are not request/response. The collector
registers a notification handler on multilspy's underlying server object (an
internal API) and stashes the latest diagnostics per file URI. Registration is
guarded so a multilspy version without that surface degrades to disabled rather
than raising.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# LSP DiagnosticSeverity -> label.
_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


class DiagnosticsCollector:
    """Per-server sink for pushed diagnostics, keyed by file URI."""

    def __init__(self) -> None:
        self._by_uri: dict[str, list[dict]] = {}
        self.enabled = False

    def attach(self, server) -> None:
        """Register the publishDiagnostics handler on ``server``'s underlying
        notification API. Best-effort: leaves ``enabled`` False if the API isn't
        present or registration fails."""
        handler = getattr(server, "server", None)
        on_notification = getattr(handler, "on_notification", None)
        if on_notification is None:
            logger.debug("multilspy server has no on_notification; diagnostics disabled")
            return
        try:
            on_notification("textDocument/publishDiagnostics", self.feed)
            self.enabled = True
        except Exception as exc:  # noqa: BLE001 — degrade, never crash a session
            logger.debug("failed to register diagnostics handler: %s", exc, exc_info=True)

    def feed(self, *args) -> None:
        """Absorb one ``textDocument/publishDiagnostics`` notification, stashing its
        diagnostics under the file URI. Public so another owner of the notification
        surface (e.g. the manager's diagnostics-wakeup wrapper) can feed the
        collector without reaching into a private method — multilspy passes the
        params positionally, so the dict is located by shape, not arg position."""
        params = next((a for a in args if isinstance(a, dict) and "uri" in a), None)
        if params is None:
            return
        self._by_uri[params["uri"]] = params.get("diagnostics") or []

    def latest(self, uri: str) -> list[dict]:
        return self._by_uri.get(uri, [])


def format_diagnostics(path: str, diags: list[dict], *, max_results: int = 50) -> str:
    """Render diagnostics as ``path:line:col: severity: message`` lines (1-based,
    first message line only). Empty list → a clear 'no diagnostics' note."""
    if not diags:
        return f"{path}: no diagnostics"
    lines: list[str] = []
    for d in diags[:max_results]:
        sev = _SEVERITY.get(d.get("severity", 1), "error")
        start = d.get("range", {}).get("start", {})
        line = start.get("line", 0) + 1
        col = start.get("character", 0) + 1
        msg = (d.get("message") or "").splitlines()
        head = msg[0] if msg else ""
        lines.append(f"{path}:{line}:{col}: {sev}: {head}")
    extra = len(diags) - max_results
    if extra > 0:
        lines.append(f"… and {extra} more")
    return "\n".join(lines)
