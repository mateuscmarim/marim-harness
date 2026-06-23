"""Execute configured hooks as subprocesses: payload JSON on stdin, exit-0 stdout
read for injected context on injection events. Reuses the process-group SIGKILL
timeout discipline from ``tools/shell.py``. Never raises."""

import asyncio
import json
import logging
import os
import re
import signal
from typing import Optional

from .events import INJECTING_EVENTS, POST_TOOL_USE, PRE_TOOL_USE

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30


def _coerce_timeout(value) -> float:
    """A hook ``timeout`` comes straight from untrusted JSON. Coerce it to a
    positive float, falling back to the default for anything non-numeric or
    non-positive — a bad value (e.g. ``"abc"`` or ``null``) must not reach
    ``asyncio.wait_for`` and raise ``TypeError`` (which would drop the hook and
    leak its already-spawned subprocess)."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return float(_DEFAULT_TIMEOUT)
    return seconds if seconds > 0 else float(_DEFAULT_TIMEOUT)


def _kill(proc) -> None:
    """SIGKILL the hook's whole process group; fall back to killing the leader."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def base_payload(
    event: str, *, session_id: str, cwd: str, transcript_path: str, **extra
) -> dict:
    """Assemble a hook payload with the common Claude-Code fields plus any
    event-specific extras."""
    payload = {
        "hook_event_name": event,
        "session_id": session_id,
        "cwd": cwd,
        "transcript_path": transcript_path,
    }
    payload.update(extra)
    return payload


def _matches(matcher, event: str, tool_name: str) -> bool:
    """``matcher`` (a regex on the tool name) gates only the tool events; for all
    other events it is ignored. Absent/empty/``*`` matches everything. Non-string
    matchers are treated as non-matching."""
    if event not in (PRE_TOOL_USE, POST_TOOL_USE):
        return True
    if not matcher or matcher == "*":
        return True
    try:
        return re.search(matcher, tool_name) is not None
    except (re.error, TypeError):
        return False


def _extract_context(out: str) -> Optional[str]:
    """Pull ``additionalContext`` from a hook's stdout: either CC's structured
    JSON or, when not JSON, the plain text verbatim."""
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return out  # plain text
    if isinstance(data, dict):
        hso = data.get("hookSpecificOutput")
        if isinstance(hso, dict) and hso.get("additionalContext"):
            return str(hso["additionalContext"])
        if data.get("additionalContext"):
            return str(data["additionalContext"])
        return None  # valid JSON, but no context field
    return out


async def _run_one(command: str, payload: dict, timeout) -> Optional[str]:
    """Run one hook command, feeding ``payload`` as JSON on stdin. Returns stripped
    stdout on a clean exit-0 run, else ``None``. Swallows every failure — logged
    at DEBUG so an operator can diagnose misconfig or a misbehaving hook."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        logger.debug("hook command %r failed to spawn: %s", command, exc)
        return None
    data = json.dumps(payload).encode()
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(input=data), timeout=_coerce_timeout(timeout)
        )
    except (asyncio.TimeoutError, OSError, ValueError) as exc:
        # Timeout or any communicate failure: reap the child so a spawned hook
        # process can never leak.
        logger.debug("hook command %r failed/timed out: %s", command, exc)
        _kill(proc)
        await proc.wait()
        return None
    if proc.returncode != 0:
        logger.debug("hook command %r exited %s", command, proc.returncode)
        return None
    return stdout.decode(errors="replace").strip() or None


class HookRunner:
    """Holds the merged hook config and dispatches events to it."""

    def __init__(self, config: dict) -> None:
        self._config = config or {}

    async def dispatch(self, event: str, payload: dict) -> Optional[str]:
        """Run every hook configured for ``event`` whose matcher passes. Returns
        injected context for injection events, else ``None``. Never raises."""
        entries = self._config.get(event)
        if not entries:
            return None
        tool_name = str(payload.get("tool_name", ""))
        contexts: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not _matches(entry.get("matcher"), event, tool_name):
                continue
            for spec in entry.get("hooks", []) or []:
                if not isinstance(spec, dict) or spec.get("type") != "command":
                    continue
                command = spec.get("command")
                if not command:
                    continue
                timeout = spec.get("timeout", _DEFAULT_TIMEOUT)
                try:
                    out = await _run_one(str(command), payload, timeout)
                except Exception as exc:
                    logger.warning("hook %r failed: %s", command, exc)
                    out = None  # belt-and-suspenders: a hook never breaks a turn
                if out and event in INJECTING_EVENTS:
                    ctx = _extract_context(out)
                    if ctx:
                        contexts.append(ctx)
        return "\n".join(contexts) if contexts else None
