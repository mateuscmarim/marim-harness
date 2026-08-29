from marim_harness.runtime.structured import validate_dict_output

SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
    "required": ["a"],
}


def test_valid_object_passes():
    assert validate_dict_output({"a": 1, "b": "x"}, SCHEMA) == []


def test_wrong_type_reported():
    errors = validate_dict_output({"a": "not-an-int"}, SCHEMA)
    assert errors
    assert any("a" in e for e in errors)


def test_missing_required_reported():
    errors = validate_dict_output({}, SCHEMA)
    assert errors
    assert any("required" in e for e in errors)


def test_non_object_output_reported():
    errors = validate_dict_output(["not", "a", "dict"], SCHEMA)
    assert errors
    assert any("not a JSON object" in e for e in errors)


def test_none_output_reported():
    errors = validate_dict_output(None, SCHEMA)
    assert errors
