"""Pure host declarations for the named agents available to workflow scripts."""

from __future__ import annotations

import builtins
import hashlib
import keyword
import re
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass

from ..workspace.agents import AgentDef

# Sandbox imports and generated type declarations must not shadow a worker.
_RESERVED = (
    frozenset(dir(builtins))
    | frozenset(keyword.kwlist)
    | {
        "asyncio",
        "json",
        "re",
        "datetime",
        "typing",
        "Any",
        "TypedDict",
        "NotRequired",
        "Literal",
    }
)
_TYPED_ALIASES = frozenset({"research_findings", "verify_claim"})

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "source": {"type": "string"},
                    "evidence_type": {"type": "string"},
                    "quality": {"type": "string"},
                    "load_bearing": {"type": "boolean"},
                },
                "required": ["claim", "source", "quality", "load_bearing"],
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings", "open_questions"],
}
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["holds", "downgrade", "refuted"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
}


@dataclass(frozen=True)
class WorkflowBinding:
    """A fixed worker contract; scripts supply only the task."""

    name: str
    agent_type: str
    output_schema: dict | None = None
    isolation: str | None = None

    def __post_init__(self) -> None:
        if not self.name.isidentifier() or self.name in _RESERVED or self.name.startswith("_"):
            raise ValueError(
                f"Workflow binding name must be a safe Python identifier: {self.name!r}"
            )
        if self.isolation not in (None, "worktree"):
            raise ValueError("Workflow binding isolation must be None or 'worktree'")
        if self.output_schema is not None:
            if self.output_schema.get("type") != "object":
                raise ValueError("Workflow output schemas must have an object root")
            if self.isolation is not None:
                raise ValueError("Structured workflow bindings cannot use worktree isolation")
            object.__setattr__(self, "output_schema", deepcopy(self.output_schema))


def _identifier(role: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", role).strip("_") or "worker"
    if name[0].isdigit() or name in _RESERVED:
        name = f"worker_{name}"
    return name


def _configured_bindings(
    roles: set[str], bindings: Sequence[WorkflowBinding]
) -> dict[str, WorkflowBinding]:
    result: dict[str, WorkflowBinding] = {}
    for binding in bindings:
        if binding.name in _TYPED_ALIASES:
            raise ValueError(
                f"Workflow binding name is reserved for a shipped contract: {binding.name}"
            )
        if binding.agent_type not in roles:
            raise ValueError(f"Unknown workflow agent role: {binding.agent_type!r}")
        if binding.name in result:
            raise ValueError(f"Duplicate workflow binding name: {binding.name!r}")
        result[binding.name] = binding
    return result


def build_workflow_bindings(
    agents: Sequence[AgentDef], bindings: Sequence[WorkflowBinding] = ()
) -> tuple[WorkflowBinding, ...]:
    """Build a deterministic catalog without loading the optional workflow runtime.

    Explicit names and shipped typed aliases take precedence over generated names;
    discovered roles are never overwritten. ``research_findings`` and ``verify_claim``
    are reserved for the shipped skill's fixed contracts. Missing typed roles are
    omitted (construction wiring can emit the diagnostic); unknown explicit roles
    are errors.
    """
    roles = {agent.qualified_name for agent in agents}
    if len(roles) != len(agents):
        raise ValueError("Duplicate qualified agent names in workflow catalog")
    result = _configured_bindings(roles, bindings)
    for name, role, schema in (
        ("research_findings", "researcher", FINDINGS_SCHEMA),
        ("verify_claim", "explore", VERDICT_SCHEMA),
    ):
        if role in roles:
            result[name] = WorkflowBinding(name, role, schema)
    for role in sorted(roles):
        name = _identifier(role)
        if name in result:
            suffix = hashlib.sha256(role.encode()).hexdigest()[:8]
            name = f"{name}_{suffix}"
        while name in result:
            name += "_"
        result[name] = WorkflowBinding(name, role)
    return tuple(result[name] for name in sorted(result))
