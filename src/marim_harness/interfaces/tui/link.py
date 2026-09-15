"""The TUI's session seam (phase 4a): one surface over an in-process
``SessionHost`` or a daemon-owned session reached over HTTP.

After 3b the app runs its turns through the host and renders only from bus
events. 4a lets the host live in another process. ``HarnessApp`` therefore
holds a ``SessionLink`` — the command surface, an event feed and a small read
model — instead of the host itself:

- ``LocalSessionLink`` wraps the in-process host and the harness. Its
  ``info`` is a live view whose properties read the harness, so nothing the
  local widgets see is copied or can drift.
- ``server.client.RemoteSessionHost`` is the same surface over REST + a
  WebSocket, with a dataclass read model seeded from ``GET session`` and
  kept current from events.

The command methods are coroutines on both: a remote submit is a round trip,
and the local wrapper awaiting nothing costs nothing. The local wrapper calls
``self.host.<method>`` at call time (never caches bound methods), so the 3b
test seams that patch ``app.host.submit`` keep working through the link.

Process-local features — anything that needs the ``Harness`` object itself —
go through ``HarnessApp.require_local(what)``, which raises ``RemoteOnly`` on
an attached TUI. The dispatcher and key actions catch it and post one notice;
no handler checks ``link.kind`` itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic_ai.usage import RunUsage

from ...compaction import estimate_tokens
from ...config.backend_state import backend_snapshot
from ...jobs import Job
from ...runtime.permissions import Mode
from ...server.client import HistorySnapshot
from ...server.jobs_view import output_preview
from ...server.schema import Event

if TYPE_CHECKING:
    from ...runtime.harness import Harness
    from ...server.host import SessionHost


class RemoteOnly(Exception):
    """A process-local feature was asked of a TUI attached to the daemon."""

    def __init__(self, what: str) -> None:
        self.what = what
        super().__init__(
            f"{what} needs the session's own process — this TUI is attached to the daemon"
        )


class Feed(Protocol):
    """What the pump consumes: ``bus.Subscription`` or the remote feed."""

    async def next_event(self, timeout: float | None = None) -> Event | None: ...
    def close(self) -> None: ...


class LinkInfo(Protocol):
    """The read model the widgets and replay need, and nothing more."""

    @property
    def workspace_root(self) -> Path: ...
    @property
    def session_id(self) -> str | None: ...
    @property
    def session_name(self) -> str | None: ...
    @property
    def mode(self) -> str: ...
    @property
    def model_id(self) -> str | None: ...
    @property
    def model_label(self) -> str: ...
    @property
    def advisor_model_id(self) -> str | None: ...
    @property
    def thinking_level_id(self) -> str | None: ...
    @property
    def usage(self) -> RunUsage: ...
    @property
    def history_tokens(self) -> int: ...
    @property
    def message_count(self) -> int: ...
    @property
    def compact_threshold(self) -> int: ...
    @property
    def duration_seconds(self) -> float: ...
    @property
    def quota_hint(self) -> Any: ...
    @property
    def context_report(self) -> Any: ...
    @property
    def model_source(self) -> Any: ...
    @property
    def backend_inventory(self) -> dict: ...
    @property
    def backend_telemetry(self) -> dict: ...


class SessionLink(Protocol):
    kind: str

    # A read-only property in the protocol (not an attribute): the concrete
    # links each hold their own info type, and a mutable protocol attribute
    # is invariant — it would refuse both.
    @property
    def info(self) -> LinkInfo: ...

    def attach(self, after_seq: int | None = None) -> Feed: ...
    async def load_session(self) -> str: ...
    async def submit(
        self,
        prompt: str,
        attachments: list[tuple[bytes, str]] | None = None,
        *,
        trigger: str = "user",
    ) -> str: ...
    async def interrupt(self) -> bool: ...
    async def steer(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None: ...
    async def answer_ask(self, ask_id: str, answer: dict) -> bool: ...
    async def pending_asks(self) -> list[dict]: ...
    async def set_mode(self, mode: str) -> None: ...
    async def set_model(self, model_id: str) -> None: ...
    async def history(self) -> HistorySnapshot: ...
    async def refresh(self) -> None: ...
    async def close(self) -> None: ...
    # Jobs (phase 4b): the read is a snapshot of the session's registry; the
    # verbs return the registry's / runner's own verdict strings so the
    # commands read identically on both kinds of link.
    async def jobs(self) -> list[Job]: ...
    async def job_output(self, job_id: str) -> str: ...
    async def cancel_job(self, job_id: str) -> str: ...
    async def resume_spawn(self, stream_id: str) -> tuple[str | None, str]: ...


class LocalLinkInfo:
    """A live view over the harness: every property reads the current value,
    so the local widgets see exactly what they saw before the seam."""

    def __init__(self, harness: Harness) -> None:
        self._harness = harness
        self._tokens_key: tuple[int, int] | None = None
        self._tokens = 0

    @property
    def workspace_root(self) -> Path:
        return self._harness.deps.workspace.root

    @property
    def session_id(self) -> str | None:
        store = self._harness.session.store
        return store.session_id if store is not None else None

    @property
    def session_name(self) -> str | None:
        return self._harness.session.session_name

    @property
    def mode(self) -> str:
        return self._harness.mode.value

    @property
    def model_id(self) -> str | None:
        return self._harness.model_id

    @property
    def model_label(self) -> str:
        return self._harness.model_label

    @property
    def advisor_model_id(self) -> str | None:
        return self._harness.advisor_model_id

    @property
    def thinking_level_id(self) -> str | None:
        return self._harness.thinking_level_id

    @property
    def usage(self) -> RunUsage:
        return self._harness.session.usage

    @property
    def history_tokens(self) -> int:
        """Memoized on (length, version): estimate_tokens serializes every
        part of every message, and the status bar reads this ~12.5×/s while
        a turn streams for a number that only moves on commit."""
        session = self._harness.session
        history = session.history
        key = (len(history), session.history_version)
        if key != self._tokens_key:
            self._tokens_key = key
            self._tokens = estimate_tokens(history)
        return self._tokens

    @property
    def message_count(self) -> int:
        return len(self._harness.session.history)

    @property
    def compact_threshold(self) -> int:
        return self._harness.session.compact_threshold

    @property
    def duration_seconds(self) -> float:
        return self._harness.session.duration_snapshot()

    @property
    def quota_hint(self) -> Any:
        return getattr(self._harness.current_model, "quota_hint", None)

    @property
    def context_report(self) -> Any:
        """The CLI backend's own context reading (``ContextReport``), or
        None under marim's own providers — then the status bar shows the
        estimate over ``history_tokens``. A resumed CLI session shows the
        persisted reading until its first new turn."""
        from ...config.context_report import current_context_report

        harness = self._harness
        return current_context_report(harness.current_model, harness.session.history)

    @property
    def model_source(self) -> Any:
        return self._harness.model_source

    @property
    def backend_inventory(self) -> dict:
        return backend_snapshot(self._harness.current_model, "backend_inventory")

    @property
    def backend_telemetry(self) -> dict:
        return backend_snapshot(self._harness.current_model, "backend_telemetry")


class LocalSessionLink:
    """The in-process link: the host's sync commands behind the async
    surface, the harness's own setters for mode/model, the bus for events."""

    kind = "local"

    def __init__(self, harness: Harness, host: SessionHost) -> None:
        self.harness = harness
        self.host = host
        self.info = LocalLinkInfo(harness)

    async def load_session(self) -> str:
        """The host's current status. In process there is nothing to seed
        (``info`` reads the harness live); the remote link re-reads
        ``GET session`` here."""
        return self.host.status

    def attach(self, after_seq: int | None = None) -> Feed:
        return self.host.bus.attach(after_seq)

    async def submit(
        self,
        prompt: str,
        attachments: list[tuple[bytes, str]] | None = None,
        *,
        trigger: str = "user",
    ) -> str:
        return self.host.submit(prompt, attachments, trigger=trigger)

    async def interrupt(self) -> bool:
        return self.host.interrupt()

    async def steer(self, text: str, attachments: list[tuple[bytes, str]] | None = None) -> None:
        self.host.steer(text, attachments)

    async def answer_ask(self, ask_id: str, answer: dict) -> bool:
        return self.host.answer_ask(ask_id, answer)

    async def pending_asks(self) -> list[dict]:
        return self.host.pending_asks()

    async def set_mode(self, mode: str) -> None:
        self.harness.set_mode(Mode(mode))

    async def set_model(self, model_id: str) -> None:
        self.harness.set_model(model_id)

    async def history(self) -> HistorySnapshot:
        return HistorySnapshot(list(self.harness.session.history), None)

    async def refresh(self) -> None:
        """Nothing to fetch: the live view already reads the harness."""

    async def close(self) -> None:
        await self.host.stop()

    async def jobs(self) -> list[Job]:
        registry = self.harness.deps.jobs
        return registry.history + registry.list()

    async def job_output(self, job_id: str) -> str:
        registry = self.harness.deps.jobs
        return output_preview(registry.get(job_id), registry.output(job_id))

    async def cancel_job(self, job_id: str) -> str:
        return await self.harness.deps.jobs.cancel(job_id)

    async def resume_spawn(self, stream_id: str) -> tuple[str | None, str]:
        resume = self.harness.deps.services.resume_subagent
        if resume is None:
            return None, "sub-agent resume is not available in this session"
        return await resume(stream_id)
