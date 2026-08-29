"""Post-turn validation for dict-schema structured output.

``StructuredDict`` attaches a JSON Schema for provider-side constrained
generation but never validates the emitted object, so a dict-schema turn
gets checked here after the run. Lives in core (not workflows/schema.py)
because core must not import the extra-gated workflows package; validator
class resolution matches it — validator_for(schema), which honors the
schema's own $schema and defaults to the latest draft.
"""

from __future__ import annotations

from typing import Any

from jsonschema.validators import validator_for


def validate_dict_output(output: Any, schema: dict) -> list[str]:
    """Validate a turn's structured output against its JSON Schema.

    Returns ``[]`` when valid, else human-readable error strings. A
    non-dict output fails against the object root rather than crashing.
    """
    if not isinstance(output, dict):
        return [f"structured output is not a JSON object: {type(output).__name__}"]
    validator = validator_for(schema)(schema)
    errors = sorted(validator.iter_errors(output), key=lambda e: list(e.path))
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
