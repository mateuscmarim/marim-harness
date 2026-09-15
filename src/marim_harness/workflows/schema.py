"""Validate full runner reports at the typed workflow boundary.

Native reports are already structured; CLI reports may need JSON extraction.
These pure helpers perform no retries or orchestration.
"""

from __future__ import annotations

import json
import re

import jsonschema
import jsonschema.validators

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
    it's used to declare a typed worker. Raises WorkflowResultError with a
    model-actionable message on a malformed schema; returns None on success.
    Catching this up front avoids spawning a sub-agent whose report can never
    validate because the schema itself is broken."""
    validator_cls = jsonschema.validators.validator_for(schema)
    try:
        validator_cls.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise WorkflowResultError(
            f"Workflow output schema is not a valid JSON Schema: {exc.message}"
        ) from exc


def validate_report(report: str, schema: dict) -> tuple[object | None, str | None]:
    """Validate a sub-agent report against its fixed output schema. Returns
    (data, None) on success or (None, reason) with a model-readable reason."""
    data = extract_json(report)
    if data is None:
        return None, "the report is not valid JSON (nor contains a JSON code block)"
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        return None, f"the JSON does not match the schema: {exc.message}"
    return data, None
