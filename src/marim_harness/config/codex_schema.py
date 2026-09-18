"""Adapt local schema pointers to the Codex/OpenAI definition-only reference dialect."""

from typing import Any
from urllib.parse import unquote

from pydantic_ai import JsonSchemaTransformer
from pydantic_ai.exceptions import UserError
from pydantic_ai.profiles.openai import OpenAIJsonSchemaTransformer


def _pointer_target(root: dict[str, Any], ref: str) -> dict[str, Any]:
    target: Any = root
    try:
        for token in unquote(ref[2:]).split("/"):
            key = token.replace("~1", "/").replace("~0", "~")
            if isinstance(target, list):
                if not key.isdecimal() or (len(key) > 1 and key.startswith("0")):
                    raise ValueError("Invalid array index")
                target = target[int(key)]
            else:
                target = target[key]
        if not isinstance(target, dict):
            raise ValueError("Target is not a schema object")
    except (KeyError, IndexError, TypeError, ValueError):
        raise UserError(f"Cannot normalize local JSON Schema reference: {ref}") from None
    return target


def _normalize_local_refs(root: dict[str, Any]) -> dict[str, Any]:
    pending: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    reserved = set(root.get("$defs", {}))

    class LocalReferences(JsonSchemaTransformer):
        def transform(self, schema: dict[str, Any]) -> dict[str, Any]:
            ref = schema.get("$ref")
            if not isinstance(ref, str) or not ref.startswith("#/"):
                return schema
            if ref.startswith("#/$defs/") and len(ref.split("/")) == 3:
                return schema
            if ref not in names:
                target = _pointer_target(root, ref)
                name = f"marim_local_ref_{len(reserved)}"
                while name in reserved:
                    name += "_"
                reserved.add(name)
                names[ref] = name
                pending[name] = target
            return {**schema, "$ref": f"#/$defs/{names[ref]}"}

    # The public walker copies schemas and visits schema positions only, leaving
    # examples/defaults/enum values alone. Register targets before walking them so
    # cycles stay finite, and drain separately from the walker's $defs iteration.
    result = LocalReferences(root).walk()
    while pending:
        name, target = pending.popitem()
        result.setdefault("$defs", {})[name] = LocalReferences(target).walk()
    return result


class CodexJsonSchemaTransformer(OpenAIJsonSchemaTransformer):
    def __init__(self, schema: dict[str, Any], *, strict: bool | None = None):
        # Upstream handles strict schemas but leaves arbitrary JSON Pointers intact;
        # its inline-defs mode only resolves #/$defs names. Hoisting preserves $ref
        # siblings and recursive schemas without a resolver or per-MCP exceptions.
        super().__init__(_normalize_local_refs(schema), strict=strict)
