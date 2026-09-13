"""The terminal payload of one user turn.

``TurnOutcome`` mirrors the Claude Agent SDK's ``ResultMessage``: the turn's
subtype, the final text (``result``), the validated structured data
(``structured_output``), failure detail (``errors``), and the turn's spend
(``usage``). ``run_turn`` returns this in all cases — plain harnesses get
``subtype="success"`` with ``structured_output=None``.

``usage`` is THIS turn's total across every model round it ran (approval
continuations, the dict-schema corrective round, and a failed round that
was retried after compaction/backoff all fold in), so an embedder can bill
a turn without diffing ``session.usage`` before and after. It is the same
accumulator the controller banks into ``session.usage`` — the session total
stays cumulative across turns, this is the per-turn slice of it. A turn
that ends in a raise (provider error, ``UsageLimitExceeded``) has no
outcome; its spend is still banked into ``session.usage``.

``error_during_execution`` is part of the vocabulary for Claude parity but
v1 never emits it: marim's tool failures feed back to the model as tool
results rather than ending the turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic_ai.usage import RunUsage

Subtype = Literal[
    "success",
    "error_max_structured_output_retries",
    "error_during_execution",
]


@dataclass(frozen=True)
class TurnOutcome:
    subtype: Subtype
    result: str | None
    structured_output: Any = None
    errors: list[str] | None = None
    usage: RunUsage = field(default_factory=RunUsage)
