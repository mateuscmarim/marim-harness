import json
from typing import Annotated, TypeVar

from pydantic import BeforeValidator

_T = TypeVar("_T")


def _decode_json(value: object) -> object:
    """Before-validator for a structured tool argument: some models serialize a
    list or object argument as a JSON *string* (e.g. ``'[{"old_string": …}]'`` or
    ``'{"text": …}'``) rather than a real array/object. Decode such a string to the
    value it represents; pass anything else through untouched, so a genuine
    array/object validates normally and a non-JSON string still surfaces the real
    validation error instead of being swallowed."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


# A ``list[T]`` tool argument that also tolerates a JSON-stringified list. The
# before-validator runs ahead of list validation; the JSON schema advertised to
# the model stays ``array`` (BeforeValidator leaves it unchanged), so a
# well-behaved model is unaffected while a lenient one doesn't fail the turn on a
# stringified array. Applied to every array-typed tool arg (edits/todos/questions).
LenientList = Annotated[list[_T], BeforeValidator(_decode_json)]

# A single tool argument (or list element) that tolerates a JSON-stringified
# object. Same relax-don't-mask contract as ``LenientList``; used on the object
# element types so a model that stringifies each element (not just the whole list)
# still validates. The advertised schema is unchanged.
Lenient = Annotated[_T, BeforeValidator(_decode_json)]
