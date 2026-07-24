"""The Advisor capability: marim's advisor pattern, exported for any
pydantic-ai agent.

Bundles one ``advisor`` tool (forwards the run's transcript to a
separately-configured reviewer model and returns strategic guidance) with
the guidance instructions telling the model when to consult it. The consult
logic itself is ``advisor.consult`` — the same core marim's own runtime
advisor uses, so the two can never drift.

Unlike marim's runtime advisor (live ``/advisor`` toggling, session
persistence, claude-cli clone handling), this capability is statically
configured: one model, fixed caps. Embedders using ``HarnessBuilder`` should
pick ONE of ``with_advisor(...)`` / ``with_capability(Advisor(...))`` —
attaching both registers two tools named ``advisor`` and pydantic-ai will
reject the duplicate at run time."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import Model, infer_model
from pydantic_ai.toolsets import FunctionToolset

from ..advisor import ADVISOR_GUIDANCE, consult


@dataclass(init=False)
class Advisor(AbstractCapability[Any]):
    """Consult a separately-configured, typically stronger model mid-task.

    ```python
    from pydantic_ai import Agent
    from marim_harness.capabilities import Advisor

    agent = Agent(
        "anthropic:claude-sonnet-4-6",
        capabilities=[Advisor(model="openai:gpt-5.2", max_uses=5)],
    )
    ```
    """

    model: Model | str
    """The advisor model. A string resolves via ``infer_model`` lazily on the
    first consultation, so constructing the capability never needs provider
    credentials."""

    max_uses: int | None
    """Per-run cap on consultations; ``None`` = unlimited."""

    max_tokens: int
    """Advice budget forwarded to the one-shot advisor run."""

    def __init__(
        self,
        model: Model | str,
        *,
        max_uses: int | None = 5,
        max_tokens: int = 2048,
        id: str | None = None,
        description: str | None = None,
        defer_loading: bool = False,
    ) -> None:
        self.id = id
        self.description = description
        self.defer_loading = defer_loading
        self.model = model
        self.max_uses = max_uses
        self.max_tokens = max_tokens
        self._uses = 0
        self._resolved: Model | None = None

    def _resolve_model(self) -> Model:
        # Lazy + cached: a string slug is only resolved (and only needs
        # credentials) when the tool is first called, per the design spec.
        if self._resolved is None:
            m = self.model
            self._resolved = m if isinstance(m, Model) else infer_model(m)
        return self._resolved

    def get_instructions(self):
        return ADVISOR_GUIDANCE

    def get_toolset(self):
        toolset: FunctionToolset[Any] = FunctionToolset()

        @toolset.tool
        async def advisor(ctx: RunContext[Any]) -> str:
            """Consult your advisor: a stronger reviewer model that sees this
            entire conversation — the task, your reasoning, and every tool
            call and result — and returns strategic guidance.

            Call it before starting substantive work on a non-trivial task,
            when you are stuck or about to make a risky change, and before
            declaring a complex task done. It takes no arguments; the
            transcript is forwarded automatically. The advice is guidance to
            weigh against your own evidence, not an instruction to follow
            blindly.
            """
            if self.max_uses is not None and self._uses >= self.max_uses:
                return (
                    f"Advisor call cap reached (max_uses={self.max_uses} per "
                    "run). Continue without advice."
                )
            try:
                model = self._resolve_model()
            except Exception as exc:
                # Errors-as-text: a broken advisor degrades the advice,
                # never the run.
                return (
                    f"Advisor unavailable: can't build model {self.model!r}: "
                    f"{exc}. Continue without advice."
                )
            self._uses += 1
            return await consult(model, list(ctx.messages), max_tokens=self.max_tokens)

        return toolset
