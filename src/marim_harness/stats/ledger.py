"""Durable JSONL ledger: dual-write (workspace + global) append, and read-back."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from ..session.store import default_sessions_base, workspace_slug
from .types import TurnEvent

logger = logging.getLogger(__name__)

__all__ = [
    "StatsLedger",
    "default_sessions_base",
    "default_stats_base",
    "event_from_dict",
    "event_to_dict",
    "iter_turns",
    "workspace_slug",
]


def default_stats_base(sessions_base: Path) -> Path:
    sessions_base = Path(sessions_base)
    if sessions_base.name == "sessions":
        return sessions_base.parent / "stats"
    return sessions_base / "stats"


def event_to_dict(event: TurnEvent) -> dict:
    return asdict(event)


def event_from_dict(data: dict) -> TurnEvent | None:
    if not isinstance(data, dict):
        return None
    if data.get("v") != 1:
        return None
    session_id = data.get("session_id")
    day = data.get("day")
    if not session_id or not day:
        return None
    try:
        return TurnEvent(
            v=1,
            ts=data.get("ts", ""),
            day=day,
            session_id=session_id,
            workspace=data.get("workspace", ""),
            model=data.get("model"),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cache_read_tokens=data.get("cache_read_tokens", 0),
            cache_write_tokens=data.get("cache_write_tokens", 0),
            cost_usd=data.get("cost_usd"),
            cost_is_exact=data.get("cost_is_exact", False),
            session_duration_seconds=data.get("session_duration_seconds"),
        )
    except (TypeError, ValueError):
        return None


def iter_turns(path: Path) -> Iterator[TurnEvent]:
    path = Path(path)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = event_from_dict(data)
        if event is not None:
            yield event


def _append_line(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(line + "\n")
            f.flush()
    except (OSError, TypeError) as exc:
        logger.warning("stats ledger: failed to append to %s: %s", path, exc)


class StatsLedger:
    """Appends one line per turn to both a per-workspace and a global JSONL
    file, and reads them back. Never raises: a write failure is logged and
    the other file is still attempted."""

    def __init__(self, stats_base: Path, workspace_slug: str) -> None:
        self.stats_base = Path(stats_base)
        self.workspace_slug = workspace_slug

    @property
    def workspace_path(self) -> Path:
        return self.stats_base / self.workspace_slug / "turns.jsonl"

    @property
    def global_path(self) -> Path:
        return self.stats_base / "global" / "turns.jsonl"

    def append(self, event: TurnEvent) -> None:
        line = json.dumps(event_to_dict(event), separators=(",", ":"))
        _append_line(self.workspace_path, line)
        _append_line(self.global_path, line)

    def iter_workspace(self) -> Iterator[TurnEvent]:
        yield from iter_turns(self.workspace_path)

    def iter_global(self) -> Iterator[TurnEvent]:
        yield from iter_turns(self.global_path)
