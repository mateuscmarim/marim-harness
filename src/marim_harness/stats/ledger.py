"""Durable JSONL ledger: dual-write (workspace + global) append, and read-back."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Literal

from ..session.store import default_sessions_base, workspace_slug
from .query import models as _models_query
from .query import overview as _overview_query
from .types import ModelsReport, Overview, Range, TurnEvent

logger = logging.getLogger(__name__)

__all__ = [
    "StatsLedger",
    "default_sessions_base",
    "default_stats_base",
    "event_from_dict",
    "event_to_dict",
    "iter_turns",
    "load_models",
    "load_overview",
    "workspace_slug",
]


def default_stats_base(sessions_base: Path) -> Path:
    sessions_base = Path(sessions_base)
    if sessions_base.name == "sessions":
        return sessions_base.parent / "stats"
    return sessions_base / "stats"


def event_to_dict(event: TurnEvent) -> dict:
    return asdict(event)


def _as_int(value: object) -> int:
    """Token counts as a plain ``int``, whatever JSON handed us.

    A ledger line is only ever read back, never trusted: a float, a numeric
    string, or ``None`` from a hand-edited or older file must not leak a
    non-``int`` into the pure query layer (where it would silently poison
    sums or raise on comparison). Anything uncoercible counts as zero.
    """
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def event_from_dict(data: object) -> TurnEvent | None:
    if not isinstance(data, dict):
        return None
    if data.get("v") != 1:
        return None
    session_id = data.get("session_id")
    day = data.get("day")
    if not isinstance(session_id, str) or not isinstance(day, str) or not session_id:
        return None
    try:
        # The query layer parses ``day`` unguarded, so a line whose day isn't
        # a real ISO date is dropped here rather than blowing up a report.
        date.fromisoformat(day)
    except ValueError:
        return None
    model = data.get("model")
    return TurnEvent(
        v=1,
        ts=str(data.get("ts", "")),
        day=day,
        session_id=session_id,
        workspace=str(data.get("workspace", "")),
        model=model if isinstance(model, str) else None,
        input_tokens=_as_int(data.get("input_tokens")),
        output_tokens=_as_int(data.get("output_tokens")),
        cache_read_tokens=_as_int(data.get("cache_read_tokens")),
        cache_write_tokens=_as_int(data.get("cache_write_tokens")),
        cost_usd=_as_float(data.get("cost_usd")),
        cost_is_exact=bool(data.get("cost_is_exact", False)),
        session_duration_seconds=_as_float(data.get("session_duration_seconds")),
    )


def iter_turns(path: Path) -> Iterator[TurnEvent]:
    """Stream events out of a ledger file, skipping anything unreadable.

    Read line-by-line (not whole-file) so a multi-megabyte ledger costs one
    line of memory, and with ``errors="replace"`` so a torn or non-UTF-8 line
    — a partial write, a truncated tail, an unrelated file at this path —
    degrades into a JSON parse failure we skip, never a ``UnicodeDecodeError``
    that would take down the whole query.
    """
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for lineno, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("stats ledger: skipping corrupt line %s:%d", path, lineno)
                    continue
                event = event_from_dict(data)
                if event is None:
                    logger.debug("stats ledger: skipping unusable event %s:%d", path, lineno)
                    continue
                yield event
    except OSError:
        return


def _append_line(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
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


def _scope_path(
    scope: Literal["workspace", "global"], stats_base: Path, workspace_slug: str | None
) -> Path:
    if scope == "workspace":
        if not workspace_slug:
            raise ValueError("workspace_slug is required when scope is 'workspace'")
        return stats_base / workspace_slug / "turns.jsonl"
    return stats_base / "global" / "turns.jsonl"


def load_overview(
    scope: Literal["workspace", "global"],
    range: Range,
    *,
    stats_base: Path,
    workspace_slug: str | None = None,
    today: date | None = None,
) -> Overview:
    path = _scope_path(scope, stats_base, workspace_slug)
    return _overview_query(iter_turns(path), range, today=today)


def load_models(
    scope: Literal["workspace", "global"],
    range: Range,
    *,
    stats_base: Path,
    workspace_slug: str | None = None,
    today: date | None = None,
) -> ModelsReport:
    path = _scope_path(scope, stats_base, workspace_slug)
    return _models_query(iter_turns(path), range, today=today)
