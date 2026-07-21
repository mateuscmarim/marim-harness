"""Execute configured hooks as subprocesses: payload JSON on stdin, exit-0 stdout
read for injected context on injection events. Reuses the process-group SIGKILL
timeout discipline from ``tools/shell.py``. Never raises."""

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
from dataclasses import dataclass

from .events import INJECTING_EVENTS, POST_COMPACT, POST_TOOL_USE, PRE_COMPACT, PRE_TOOL_USE

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30


@dataclass(frozen=True)
class HookVerdict:
    """Outcome of a verdict dispatch (PreCompact only). ``blocked`` is honored
    by the caller only for manual triggers; a crash, timeout, or nonzero exit
    other than 2 is never a block (the swallow-and-log contract holds)."""

    blocked: bool = False
    reason: str = ""


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
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


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


def _matches(matcher, event: str, subject: str) -> bool:
    """``matcher`` (a regex on ``subject``) gates the tool events (subject = tool
    name) and the compact events, PreCompact/PostCompact (subject = trigger,
    "manual"/"auto" — Claude Code's compact-event matcher semantics); for all
    other events it is ignored. Absent/empty/``*`` matches everything.
    Non-string matchers are treated as non-matching.

    The match is anchored (``re.fullmatch``) to mirror Claude Code's contract: the
    matcher must match the *whole* subject. An unanchored ``re.search`` over-matches
    — ``"Edit"`` would fire for ``"MultiEdit"`` — which is not the documented
    semantics this module claims to reproduce."""
    if event not in (PRE_TOOL_USE, POST_TOOL_USE, PRE_COMPACT, POST_COMPACT):
        return True
    if not matcher or matcher == "*":
        return True
    try:
        return re.fullmatch(matcher, subject) is not None
    except (re.error, TypeError):
        return False


def _extract_context(out: str) -> str | None:
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


async def _run_one(command: str, payload: dict, timeout) -> str | None:
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


async def _run_one_verdict(command: str, payload: dict, timeout) -> HookVerdict:
    """Run one hook for a verdict. Exit 2 blocks (stderr = reason); exit 0 with
    ``{"decision": "block"}`` on stdout blocks; everything else — including
    spawn failure, timeout, and other exit codes — allows."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        logger.debug("hook command %r failed to spawn: %s", command, exc)
        return HookVerdict()
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(payload).encode()),
            timeout=_coerce_timeout(timeout),
        )
    except (asyncio.TimeoutError, OSError, ValueError) as exc:
        logger.debug("hook command %r failed/timed out: %s", command, exc)
        _kill(proc)
        await proc.wait()
        return HookVerdict()
    if proc.returncode == 2:
        return HookVerdict(blocked=True, reason=stderr.decode(errors="replace").strip())
    if proc.returncode != 0:
        logger.debug("hook command %r exited %s", command, proc.returncode)
        return HookVerdict()
    out = stdout.decode(errors="replace").strip()
    if out:
        try:
            data = json.loads(out)
        except ValueError:
            return HookVerdict()
        if isinstance(data, dict) and data.get("decision") == "block":
            return HookVerdict(blocked=True, reason=str(data.get("reason", "")))
    return HookVerdict()


async def _run_entry(entry: object, event: str, payload: dict, subject: str) -> list[str]:
    """Run one config entry's command hooks whose matcher passes; return any
    injected contexts (empty for non-injecting events or no output)."""
    if not isinstance(entry, dict):
        return []
    if not _matches(entry.get("matcher"), event, subject):
        return []
    contexts: list[str] = []
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
    return contexts


class HookRunner:
    """Holds the merged hook config and dispatches events to it."""

    def __init__(self, config: dict) -> None:
        self._config = config or {}

    async def dispatch(self, event: str, payload: dict) -> str | None:
        """Run every hook configured for ``event`` whose matcher passes. Returns
        injected context for injection events, else ``None``. Never raises."""
        entries = self._config.get(event)
        if not entries:
            return None
        # Compact events (PreCompact/PostCompact) have no tool_name — they ride
        # the same matcher slot with their trigger ("manual"/"auto") instead,
        # per the hook design. Fall back to it so a matchered compact-event
        # entry isn't silently starved of a subject on this observe-only path
        # (dispatch_verdict already does this for the verdict path).
        subject = str(payload.get("tool_name") or payload.get("trigger", ""))
        contexts: list[str] = []
        for entry in entries:
            contexts.extend(await _run_entry(entry, event, payload, subject))
        return "\n".join(contexts) if contexts else None

    async def dispatch_verdict(self, event: str, payload: dict) -> HookVerdict:
        """Run ``event``'s hooks for a block/allow verdict. The matcher subject
        is the payload's ``trigger`` (Claude Code matches PreCompact hooks on
        "manual"/"auto", not on a tool name). All matching hooks run; the first
        block wins but later hooks still execute (observability). Never raises."""
        entries = self._config.get(event)
        if not entries:
            return HookVerdict()
        trigger = str(payload.get("trigger", ""))
        verdict = HookVerdict()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not _matches(entry.get("matcher"), event, trigger):
                continue
            for spec in entry.get("hooks", []) or []:
                if not isinstance(spec, dict) or spec.get("type") != "command":
                    continue
                command = spec.get("command")
                if not command:
                    continue
                try:
                    v = await _run_one_verdict(
                        str(command), payload, spec.get("timeout", _DEFAULT_TIMEOUT)
                    )
                except Exception as exc:
                    logger.warning("hook %r failed: %s", command, exc)
                    continue
                if v.blocked and not verdict.blocked:
                    verdict = v
        return verdict
