"""The terminal payload of one user turn.

``TurnOutcome`` mirrors the Claude Agent SDK's ``ResultMessage``: the turn's
subtype, the final text (``result``), the validated structured data
(``structured_output``), and failure detail (``errors``). ``run_turn``
returns this in all cases — plain harnesses get ``subtype="success"`` with
``structured_output=None``.

``error_during_execution`` is part of the vocabulary for Claude parity but
v1 never emits it: marim's tool failures feed back to the model as tool
results rather than ending the turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

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
