"""Per-run context masking for sub-agents.

A sub-agent's context is dominated by tool observations (file reads, grep dumps,
command output) whose useful lifespan is short: once the model has acted on an
observation, the raw payload is dead weight. ``ObservationMasker`` watches the
outgoing request size and, past a trigger, swaps stale observation payloads for
:data:`~marim_harness.compaction.MASKED_OBSERVATION` — the model keeps the
*trace* of what it did and can re-run a tool if it still needs a masked output.

The masker is deliberately **stateful, one instance per spawn**. Whether a
``ProcessHistory`` rewrite persists between requests is an upstream
implementation detail: pydantic-ai currently writes the processed history back
into the run's state (``ctx.state.message_history[:] = messages`` in
``_agent_graph._prepare_request``), but that semantics has differed across
versions, and under a request-only semantics a stateless "mask everything older
than the newest N" would mask against a boundary that moves every request —
rewriting the request prefix every time and busting the provider prompt cache
on each call. The masker therefore remembers which returns it masked (by
``tool_call_id``) and re-applies exactly that set, only *extending* it when the
estimate crosses the trigger again. Between trigger events the request prefix
is byte-stable under either upstream semantics, so masking costs one cache miss
per trigger — the same bargain session compaction makes. That bound holds only
while masking actually brings the estimate back under the trigger: in the
saturated regime (keep_recent recent returns alone keep the estimate above it),
every request re-crosses the trigger and the set grows by roughly one return
per request — the one that just aged out of the keep_recent window — so the
cost degrades to a cache miss per request. The damage stays confined to the
last ~keep_recent tool rounds near the tail, though: the long masked prefix in
front of them is already committed and remains byte-stable. A side effect of
the current write-back: the run's ``all_messages()`` and saved transcripts
carry the masked placeholders — i.e. what the model actually saw.
"""

from __future__ import annotations

import dataclasses

from pydantic_ai.messages import ModelMessage, ToolReturnPart

from ..compaction import MASKED_OBSERVATION, estimate_tokens, mask_stale_observations


class ObservationMasker:
    """Masks stale tool observations in a sub-agent's outgoing requests.

    Build one per spawn and register its :meth:`mask` as a ``ProcessHistory``
    capability. ``trigger_tokens`` is the masking trigger, already carrying any
    window safety ratio; ``keep_recent``/``min_chars`` have the semantics of
    :func:`marim_harness.compaction.mask_stale_observations`.
    """

    def __init__(self, trigger_tokens: int, keep_recent: int = 4,
                 min_chars: int = 200) -> None:
        # The trigger arrives pre-derived (min(budget, 0.8 × window) — see
        # config/context_limits.py). No internal ratio on top: stacking one
        # would silently move masking to 0.6 of the window.
        self._trigger_tokens = trigger_tokens
        self._keep_recent = keep_recent
        self._min_chars = min_chars
        # tool_call_ids whose returns are masked. Monotonic — ids are only ever
        # added — which is what keeps the request prefix stable between triggers.
        self._masked_ids: set[str] = set()

    def mask(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        """The ProcessHistory hook: re-apply the committed mask set, then extend
        it (sparing the newest ``keep_recent`` returns) if the request would still
        run past the trigger. Never mutates ``messages`` or its parts."""
        view = self._apply(messages)
        if estimate_tokens(view) <= self._trigger_tokens:
            return view
        view, masked = mask_stale_observations(
            view, self._keep_recent, min_chars=self._min_chars
        )
        if masked:
            self._commit(view)
        return view

    def _apply(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        """Rebuild ``messages`` with every return in the committed set masked.
        Returns a fresh list; untouched messages are shared, not copied."""
        out = list(messages)
        if not self._masked_ids:
            return out
        for idx, message in enumerate(out):
            parts = getattr(message, "parts", None)
            if not parts:
                continue
            new_parts = list(parts)
            changed = False
            for pidx, part in enumerate(parts):
                if (
                    isinstance(part, ToolReturnPart)
                    and part.tool_call_id in self._masked_ids
                    and part.content != MASKED_OBSERVATION
                ):
                    new_parts[pidx] = dataclasses.replace(
                        part, content=MASKED_OBSERVATION
                    )
                    changed = True
            if changed:
                out[idx] = dataclasses.replace(message, parts=new_parts)
        return out

    def _commit(self, view: list[ModelMessage]) -> None:
        """Record every masked return in ``view`` so later requests re-apply the
        exact same set."""
        for message in view:
            for part in getattr(message, "parts", []):
                if (
                    isinstance(part, ToolReturnPart)
                    and part.content == MASKED_OBSERVATION
                ):
                    self._masked_ids.add(part.tool_call_id)
