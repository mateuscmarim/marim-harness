import pytest

from marim_harness.workflows.errors import WorkflowResultError
from marim_harness.workflows.schema import (
    check_valid_schema,
    extract_json,
    shape_result,
    validate_report,
)

FINDINGS = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
    "required": ["findings"],
}


def test_extract_json_parses_a_bare_json_report():
    assert extract_json('{"findings": []}') == {"findings": []}


def test_extract_json_falls_back_to_a_fenced_block():
    report = 'Here you go:\n```json\n{"findings": ["a"]}\n```\nDone.'
    assert extract_json(report) == {"findings": ["a"]}


def test_extract_json_returns_none_for_prose():
    assert extract_json("I could not find anything.") is None


def test_validate_report_accepts_matching_json():
    data, err = validate_report('{"findings": ["x"]}', FINDINGS)
    assert err is None
    assert data == {"findings": ["x"]}


def test_validate_report_names_the_schema_violation():
    data, err = validate_report('{"findings": "not-a-list"}', FINDINGS)
    assert data is None
    assert err is not None and "not-a-list" in err


def test_validate_report_flags_non_json():
    data, err = validate_report("no json here", FINDINGS)
    assert data is None
    assert err is not None and "JSON" in err


def test_shape_result_serializes_plain_data():
    text, spill = shape_result({"n": 1}, 1000, "out.json")
    assert '"n": 1' in text
    assert spill is None


def test_shape_result_caps_oversized_output_with_a_pointer():
    big = {"rows": ["x" * 50] * 200}
    text, spill = shape_result(big, 200, ".marim/workflow-output/t.json")
    assert len(text) <= 200
    assert ".marim/workflow-output/t.json" in text
    assert spill is not None


def test_shape_result_rejects_non_serializable_values():
    with pytest.raises(WorkflowResultError):
        shape_result(object(), 1000, "out.json")


def test_shape_result_rejects_none_as_likely_accidental():
    with pytest.raises(WorkflowResultError, match="LAST EXPRESSION"):
        shape_result(None, 1000, "out.json")


def test_shape_result_accepts_other_falsy_values():
    for falsy in (0, "", [], {}, False):
        text, spill = shape_result(falsy, 1000, "out.json")
        assert spill is None
        assert text


def test_check_valid_schema_accepts_a_valid_schema():
    assert check_valid_schema(FINDINGS) is None


def test_check_valid_schema_rejects_a_malformed_schema():
    with pytest.raises(WorkflowResultError) as exc_info:
        check_valid_schema({"type": "not-a-real-type"})
    assert "not a valid JSON Schema" in str(exc_info.value)
