"""Expand a CLI provider's recorded tool activity into real history messages.

Under ``claude-cli`` / ``codex-cli`` the CLI runs its own tools, so its calls
never enter the model loop: the streamed ``ModelResponse`` is text-only and
the activity rides in ``provider_details["cli_activity"]`` (an
``ActivityLedger``, see ``config.external_cli``). At the controller's persist
points that ledger is expanded here into the same shape marim's own tools
leave behind::

    ModelResponse[prose…, ToolCallPart]  ->  ModelRequest[ToolReturnPart]  ->  ModelResponse[prose…]

so TUI replay, ``GET .../history``, compaction/masking and a mid-session
provider switch all see the CLI's tools with no special case. The
resumability invariants hold by construction: a call whose result never
arrived (the CLI turn was interrupted mid-tool) gets a synthesized return, a
result with no matching call is dropped, and the ledger key is stripped from
the expanded messages so a second pass is a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponsePart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import RequestUsage

from ..config.external_cli import CLI_ACTIVITY_KEY
from ..config.lifecycle import notice_part

MISSING_RESULT_NOTE = (
    "No result was recorded for this tool call: the CLI turn ended before the tool returned."
)
# A spawn the turn ended without answering is the normal shape of Claude
# Code's background Agent, not a cut-off tool: the report reaches the
# conversation in a later turn (the CLI's own turn on the task notification).
BACKGROUND_SPAWN_NOTE = (
    "The sub-agent runs in the background; its report arrives as a task "
    "notification in a later turn."
)
# The tool the sub-agent demux synthesizes for a Claude-side Agent/Task spawn.
_SPAWN_TOOL = "spawn_agent"


def unanswered_note(tool_name: str) -> str:
    """The synthesized return for a call the sealed turn never answered."""
    return BACKGROUND_SPAWN_NOTE if tool_name == _SPAWN_TOOL else MISSING_RESULT_NOTE


def expand_cli_activity(history: list[ModelMessage]) -> list[ModelMessage]:
    """``history`` with every ledger-carrying response split into tool-call /
    tool-return messages. Messages without a ledger pass through untouched
    (same objects), so an API-model history is returned byte-identical."""
    out: list[ModelMessage] = []
    for msg in history:
        entries = _ledger_of(msg)
        if entries is None:
            out.append(msg)
        else:
            out.extend(_Expansion(msg, entries).run())  # type: ignore[arg-type]
    return out


def _ledger_of(msg: ModelMessage) -> list[dict[str, Any]] | None:
    if not isinstance(msg, ModelResponse) or not msg.provider_details:
        return None
    entries = msg.provider_details.get(CLI_ACTIVITY_KEY)
    return entries if isinstance(entries, list) else None


class _Expansion:
    """One response's split, threaded through the ledger walk (the open
    response / open request and the unanswered calls all mutate across
    entries, so they live on a small object rather than a tangle of locals).

    Shape rules, in stream order:

    - a ``part`` marker places the source part into the open response;
    - a ``call`` joins the open response (parallel calls share one response);
    - the first ``result`` seals the open response and opens the request that
      answers it; further results of the same batch join that request;
    - anything else arriving while a request is open (a part, a call) seals
      the request — a sealed response's call still unanswered at that point
      can never be answered later, so its return is synthesized there.
    """

    def __init__(self, msg: ModelResponse, entries: list[dict[str, Any]]) -> None:
        self._msg = msg
        self._entries = entries
        self._parts = list(msg.parts)
        self._cursor = 0  # next source part not yet placed
        self._out: list[ModelMessage] = []
        self._response: list[ModelResponsePart] = []
        self._unanswered: dict[str, str] = {}  # call id -> tool name (open response)
        self._request: list[ToolReturnPart] | None = None

    def run(self) -> list[ModelMessage]:
        for entry in self._entries:
            kind = entry.get("kind")
            if kind == "part":
                self._take_parts_through(int(entry.get("index", -1)))
            elif kind == "call":
                self._call(entry)
            elif kind == "result":
                self._result(entry)
            elif kind == "notice" and isinstance(entry.get("notice"), dict):
                self._notice(entry["notice"])
        self._take_parts_through(len(self._parts) - 1)
        if self._unanswered:
            self._begin_request()
        self._close_request()
        self._close_response()
        return self._with_usage_on_last()

    def _notice(self, payload: dict) -> None:
        # A notice between parallel tool results must not seal their request:
        # that would synthesize the still-pending returns and drop real results.
        # ToolReturnPart metadata is application-only and stays out of model input.
        if self._request:
            part = self._request[-1]
            metadata = dict(part.metadata) if isinstance(part.metadata, dict) else {}
            notices = metadata.setdefault("backend_notices_after", [])
            notices.append(payload)
            part.metadata = metadata
        else:
            self._response.append(notice_part(payload))

    # --- entries ---------------------------------------------------------------------
    def _take_parts_through(self, index: int) -> None:
        """Place the source parts up to and including ``index`` into the open
        response (the ledger's part markers are monotonic, so this is a
        cursor advance; a marker past the end is simply ignored)."""
        while self._cursor <= index and self._cursor < len(self._parts):
            part = self._parts[self._cursor]
            self._cursor += 1
            if isinstance(part, TextPart) and not part.content:
                continue  # a bootstrapped part that never got its prose
            if self._unanswered:
                # Prose after a call whose result never came: answer the
                # call first so the prose starts a clean response.
                self._begin_request()
            self._close_request()
            self._response.append(part)

    def _call(self, entry: dict[str, Any]) -> None:
        self._close_request()
        call_id = str(entry.get("id", ""))
        name = str(entry.get("name") or "tool")
        self._response.append(
            ToolCallPart(tool_name=name, args=entry.get("args"), tool_call_id=call_id)
        )
        self._unanswered[call_id] = name

    def _result(self, entry: dict[str, Any]) -> None:
        call_id = str(entry.get("id", ""))
        if call_id not in self._unanswered:
            return  # orphan (no call recorded, or already answered): drop
        name = self._unanswered.pop(call_id)
        self._begin_request()
        outcome = entry.get("outcome")
        assert self._request is not None
        self._request.append(
            ToolReturnPart(
                tool_name=name,
                content=str(entry.get("content", "")),
                tool_call_id=call_id,
                timestamp=self._msg.timestamp,
                outcome=outcome if outcome in _OUTCOMES else "success",
            )
        )

    # --- message boundaries ----------------------------------------------------------
    def _begin_request(self) -> None:
        """Seal the open response and open the request that answers it."""
        if self._request is None:
            self._close_response()
            self._request = []

    def _close_response(self) -> None:
        if self._response:
            self._out.append(self._stripped(self._response))
            self._response = []

    def _close_request(self) -> None:
        """Seal the open request, synthesizing a return for every call of
        the sealed response still unanswered — the persisted history never
        leaves a dangling call (the resumability invariant every provider
        enforces)."""
        if self._request is None:
            return
        for call_id, name in self._unanswered.items():
            self._request.append(
                ToolReturnPart(
                    tool_name=name,
                    content=unanswered_note(name),
                    tool_call_id=call_id,
                    timestamp=self._msg.timestamp,
                    # A background spawn was not cut short — it simply
                    # reports later — but its card has no result yet either.
                    outcome="interrupted" if name != _SPAWN_TOOL else "success",
                )
            )
        self._unanswered.clear()
        self._out.append(
            ModelRequest(
                parts=list(self._request),
                timestamp=self._msg.timestamp,
                run_id=self._msg.run_id,
            )
        )
        self._request = None

    # --- output ----------------------------------------------------------------------
    def _stripped(self, parts: Sequence[ModelResponsePart]) -> ModelResponse:
        """A copy of the source response holding ``parts``, without the
        ledger key and (for now) without usage."""
        details = {
            k: v for k, v in (self._msg.provider_details or {}).items() if k != CLI_ACTIVITY_KEY
        }
        return replace(
            self._msg, parts=list(parts), provider_details=details or None, usage=RequestUsage()
        )

    def _with_usage_on_last(self) -> list[ModelMessage]:
        """The turn's usage rides on the LAST split only, so it is counted
        once (``last_request_input_tokens`` reads the last response). An
        expansion that produced nothing (an empty response) keeps the
        source message, minus the ledger, rather than dropping a turn."""
        if not self._out:
            return [replace(self._stripped(self._msg.parts), usage=self._msg.usage)]
        for i in range(len(self._out) - 1, -1, -1):
            msg = self._out[i]
            if isinstance(msg, ModelResponse):
                self._out[i] = replace(msg, usage=self._msg.usage)
                break
        return self._out


_OUTCOMES = ("success", "failed", "denied", "interrupted")
