from marim_harness.tools.coerce import coerce_by_schema

_OBJ = {"type": "object", "properties": {"n": {"type": "integer"}}}


def test_decodes_stringified_nested_object():
    schema = {"type": "object", "properties": {"cfg": _OBJ}}
    out = coerce_by_schema({"cfg": '{"n": 5}'}, schema)
    assert out == {"cfg": {"n": 5}}


def test_decodes_stringified_array_of_objects():
    schema = {
        "type": "object",
        "properties": {"suites": {"type": "array", "items": _OBJ}},
    }
    out = coerce_by_schema({"suites": '[{"n": 1}, {"n": 2}]'}, schema)
    assert out == {"suites": [{"n": 1}, {"n": 2}]}


def test_resolves_ref_before_decoding():
    schema = {
        "type": "object",
        "$defs": {"Cfg": _OBJ},
        "properties": {"cfg": {"$ref": "#/$defs/Cfg"}},
    }
    out = coerce_by_schema({"cfg": '{"n": 7}'}, schema)
    assert out == {"cfg": {"n": 7}}


def test_decodes_under_nullable_anyof():
    schema = {
        "type": "object",
        "properties": {
            "items": {"anyOf": [{"type": "array", "items": _OBJ}, {"type": "null"}]}
        },
    }
    out = coerce_by_schema({"items": '[{"n": 3}]'}, schema)
    assert out == {"items": [{"n": 3}]}


def test_leaves_genuine_string_field_untouched():
    schema = {"type": "object", "properties": {"note": {"type": "string"}}}
    out = coerce_by_schema({"note": '{"a": 1}'}, schema)
    assert out == {"note": '{"a": 1}'}  # a string field keeps its JSON-looking text


def test_leaves_unparseable_string_untouched():
    schema = {"type": "object", "properties": {"cfg": _OBJ}}
    out = coerce_by_schema({"cfg": "not json at all"}, schema)
    assert out == {"cfg": "not json at all"}


def test_missing_or_nondict_schema_is_identity():
    assert coerce_by_schema({"x": 1}, {}) == {"x": 1}
    assert coerce_by_schema({"x": 1}, None) == {"x": 1}


def test_real_structure_passes_through_equal():
    schema = {
        "type": "object",
        "properties": {"suites": {"type": "array", "items": _OBJ}},
    }
    val = {"suites": [{"n": 1}]}
    assert coerce_by_schema(val, schema) == val


def test_coerces_scalar_number_and_bool():
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "ok": {"type": "boolean"}},
    }
    assert coerce_by_schema({"n": "5", "ok": "true"}, schema) == {"n": 5, "ok": True}


def test_anyof_with_string_branch_leaves_value_untouched():
    schema = {
        "type": "object",
        "properties": {"id": {"anyOf": [{"type": "integer"}, {"type": "string"}]}},
    }
    # "123" is a valid string for this union — must NOT be rewritten to int 123.
    assert coerce_by_schema({"id": "123"}, schema) == {"id": "123"}


def test_list_form_union_with_string_leaves_value_untouched():
    schema = {"type": "object", "properties": {"id": {"type": ["integer", "string"]}}}
    assert coerce_by_schema({"id": "123"}, schema) == {"id": "123"}


def test_oneof_nullable_object_still_decodes():
    schema = {
        "type": "object",
        "properties": {
            "cfg": {
                "oneOf": [
                    {"type": "object", "properties": {"n": {"type": "integer"}}},
                    {"type": "null"},
                ]
            }
        },
    }
    assert coerce_by_schema({"cfg": '{"n": 4}'}, schema) == {"cfg": {"n": 4}}
