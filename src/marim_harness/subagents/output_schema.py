"""How a schema'd spawn enforces its output schema.

The native path uses pydantic-ai structured output: SubagentRunner.build
gives the child ``output_type=StructuredDict(schema)``, so a mismatch is
retried IN-RUN (the model re-emits just the final output) instead of costing
the caller a full re-spawn. Two spawn shapes can't take an output type and
fall back to a contract paragraph appended to the task text: the claude-cli
backend (an external process marim only launches) and schemas without an
object root (StructuredDict requires one). ``resolve_output_schema`` is that
decision, made once by the runner — the component that knows the backend.

Lives under ``subagents/`` because the runner owns output configuration;
full CLI report validation stays at the workflow bridge boundary."""

from __future__ import annotations

import json


def output_contract(schema: dict) -> str:
    """The output-contract paragraph appended to a schema'd task: the
    sub-agent must respond with ONLY a JSON object matching the schema."""
    return (
        "\n\nOutput contract: respond with ONLY a JSON object matching this "
        "JSON Schema — no prose before or after it:\n" + json.dumps(schema, indent=2)
    )


def resolve_output_schema(schema: dict | None, backend: str | None) -> tuple[dict | None, str]:
    """Decide the enforcement path for a spawn's output schema. Returns
    ``(schema, "")`` when the spawn can ride structured output (native
    backend, object-rooted schema),
    or ``(None, contract)`` for the prompt fallback (claude-cli backend, or a
    non-object-rooted schema on the native backend). Pure; unit-tested
    directly."""
    if schema is None:
        return None, ""
    if backend == "claude-cli" or schema.get("type") != "object":
        return None, output_contract(schema)
    return schema, ""
