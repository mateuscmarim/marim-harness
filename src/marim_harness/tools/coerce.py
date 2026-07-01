"""Schema-directed coercion of stringified tool arguments.

Some models serialize a structured tool argument as a JSON *string* — e.g. a
Playwright MCP tool's ``suites`` array arrives as ``'[{"title": …}]'`` rather than
a real array. :func:`coerce_by_schema` walks a JSON Schema alongside a value and,
*only* where the schema at that position expects a non-string type, decodes such a
string with ``json.loads`` and recurses into the result. A string where the schema
says ``string`` is left untouched; a string that won't parse is left untouched so
the downstream validator (own tools) or MCP server still raises the *real* error
instead of it being swallowed. Pure and side-effect-free — unit-tested directly.

This is the same "relax acceptance, never mask an error" contract as the
``LenientList`` before-validator in ``provider.py``; it just applies it by walking
a schema (which MCP tools expose as ``inputSchema``) rather than a pydantic type.
"""

from __future__ import annotations

import json

# JSON Schema types for which a bare string is worth trying to decode.
_NON_STRING = frozenset({"object", "array", "number", "integer", "boolean"})


def _maybe_decode(value: object) -> tuple[object, bool]:
    """``(json.loads(value), True)`` when ``value`` is a parseable JSON string, else
    ``(value, False)``."""
    if isinstance(value, str):
        try:
            return json.loads(value), True
        except (ValueError, TypeError):
            return value, False
    return value, False


def _schema_types(schema: dict) -> set[str]:
    """The declared ``type`` of a schema node as a set (JSON Schema allows a list),
    or an empty set when absent."""
    t = schema.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    return set()


def _resolve(schema: dict, defs: dict) -> dict:
    """Follow a local ``$ref`` (``#/$defs/Name`` or ``#/definitions/Name``) one or
    more hops; return ``schema`` unchanged when it has no resolvable ref."""
    seen: set[str] = set()
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or ref in seen:
            return schema
        seen.add(ref)
        target = defs.get(ref.rsplit("/", 1)[-1])
        if not isinstance(target, dict):
            return schema
        schema = target
    return schema


def coerce_by_schema(value: object, schema: object, defs: dict | None = None) -> object:
    """Return ``value`` with stringified JSON decoded wherever ``schema`` expects a
    non-string type, recursing into decoded structures. Never raises; an unknown,
    absent, or non-dict schema passes the value through unchanged."""
    if not isinstance(schema, dict):
        return value
    if defs is None:
        defs = {}
        for key in ("$defs", "definitions"):
            found = schema.get(key)
            if isinstance(found, dict):
                defs = {**defs, **found}
    schema = _resolve(schema, defs)

    # Combinators: a nullable/union schema. Use the first non-null branch — for the
    # ubiquitous ``[{...}, {"type": "null"}]`` pattern that is the real type.
    for combinator in ("anyOf", "oneOf"):
        branches = schema.get(combinator)
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, dict) and "null" not in _schema_types(branch):
                    return coerce_by_schema(value, branch, defs)
            return value

    types = _schema_types(schema)
    if types & _NON_STRING and "string" not in types:
        decoded, ok = _maybe_decode(value)
        if ok:
            value = decoded  # fall through and recurse into the decoded structure

    if isinstance(value, dict):
        props = schema.get("properties")
        addl = schema.get("additionalProperties")
        if not isinstance(props, dict) and not isinstance(addl, dict):
            return value
        result = {}
        for k, v in value.items():
            sub = props.get(k) if isinstance(props, dict) else None
            if sub is None and isinstance(addl, dict):
                sub = addl
            result[k] = coerce_by_schema(v, sub, defs) if isinstance(sub, dict) else v
        return result

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            return [coerce_by_schema(v, items, defs) for v in value]
        return value

    return value
