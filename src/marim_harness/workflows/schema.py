"""Pure helpers for workflow scripts: report validation and result shaping.
No I/O — the engine owns all effects (spawning, spill writes, UI callbacks)."""

from __future__ import annotations

import json
import re

import jsonschema
import jsonschema.validators

from ..workspace.agents import cap_subagent_output
from .errors import WorkflowResultError

_FENCED = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def extract_json(report: str) -> object | None:
    """The report's JSON payload: the whole report if it parses, else the
    first fenced block that does (models often fence despite instructions).
    None when nothing parses."""
    try:
        return json.loads(report)
    except ValueError:
        pass
    for match in _FENCED.finditer(report):
        try:
            return json.loads(match.group(1))
        except ValueError:
            continue
    return None


def check_valid_schema(schema: dict) -> None:
    """Validate that ``schema`` is itself a well-formed JSON Schema, before
    it's used to gate an agent() call. Raises WorkflowResultError with a
    model-actionable message on a malformed schema; returns None on success.
    Catching this up front avoids spawning a sub-agent whose report can never
    validate because the schema itself is broken."""
    validator_cls = jsonschema.validators.validator_for(schema)
    try:
        validator_cls.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise WorkflowResultError(
            f"agent(schema=...) is not a valid JSON Schema: {exc.message}"
        ) from exc


def validate_report(report: str, schema: dict) -> tuple[object | None, str | None]:
    """Validate a sub-agent report against the agent() schema. Returns
    (data, None) on success or (None, reason) with a model-readable reason."""
    data = extract_json(report)
    if data is None:
        return None, "the report is not valid JSON (nor contains a JSON code block)"
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        return None, f"the JSON does not match the schema: {exc.message}"
    return data, None


def shape_result(value: object, max_chars: int, spill_path: str) -> tuple[str, str | None]:
    """Serialize the script's final expression for the tool result, capping
    with the same lossless head-plus-pointer spill spawn reports use. Returns
    (text, spill): spill is the full serialization for the caller to persist
    at spill_path, or None when under budget."""
    if value is None:
        raise WorkflowResultError(
            "the workflow's final expression is None. This usually means the "
            "script ended on a statement (e.g. print(result), asyncio.run(...)) "
            "instead of a bare expression — the tool returns whatever the LAST "
            "EXPRESSION evaluates to. If you have a value to report, end the "
            "script with it directly (e.g. just `result`), not print(result)."
        )
    try:
        text = json.dumps(value, indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise WorkflowResultError(
            "the workflow's final expression is not JSON-serializable "
            f"({exc}); end the script with plain data — dicts, lists, "
            "strings, numbers"
        ) from exc
    return cap_subagent_output(text, max_chars, spill_path)
