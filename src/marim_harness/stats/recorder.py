"""Turn-usage recording into the stats ledger."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from pydantic_ai.usage import RunUsage

from ..usage import resolve_cost
from .ledger import StatsLedger
from .types import TurnEvent

logger = logging.getLogger(__name__)

__all__ = ["LedgerStatsRecorder", "NullStatsRecorder", "StatsRecorder"]


class StatsRecorder(Protocol):
    def record(self, delta: RunUsage) -> None: ...


class NullStatsRecorder:
    def record(self, delta: RunUsage) -> None:
        return


class LedgerStatsRecorder:
    """Turns a per-step :class:`RunUsage` delta into a :class:`TurnEvent` and
    appends it to the ledger. Never raises: a failure is logged and dropped,
    since a stats write must never break a turn."""

    def __init__(
        self,
        ledger: StatsLedger,
        *,
        session_id: str,
        get_model_id: Callable[[], str | None],
        get_duration_seconds: Callable[[], float | None],
    ) -> None:
        self._ledger = ledger
        self._session_id = session_id
        self._get_model_id = get_model_id
        self._get_duration = get_duration_seconds

    def set_session_id(self, session_id: str) -> None:
        """Repoint this recorder at a different session (session switch/new/
        resume swaps ``SessionController.store``, and events must attribute
        to the now-active session, not the one this recorder was built for)."""
        self._session_id = session_id

    def record(self, delta: RunUsage) -> None:
        try:
            inp = int(delta.input_tokens or 0)
            out = int(delta.output_tokens or 0)
            if inp + out == 0:
                return
            model = self._get_model_id()
            cost, exact = resolve_cost(delta, model)
            now = datetime.now(timezone.utc)
            event = TurnEvent(
                v=1,
                ts=now.isoformat(),
                day=now.date().isoformat(),
                session_id=self._session_id,
                workspace=self._ledger.workspace_slug,
                model=model,
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=int(delta.cache_read_tokens or 0),
                cache_write_tokens=int(delta.cache_write_tokens or 0),
                cost_usd=cost,
                cost_is_exact=bool(exact),
                session_duration_seconds=self._get_duration(),
            )
            self._ledger.append(event)
        except Exception:
            logger.exception("stats recorder failed; dropping event")
