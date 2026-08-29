import pytest
from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel

from marim_harness import BuilderError, Deps, HarnessBuilder


class Report(BaseModel):
    summary: str


def test_build_accepts_basemodel_schema(tmp_path):
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).with_output_type(Report).build()
    # Harness does not retain its config; the TurnController is where the
    # schema lands (Task 4 reads it).
    assert h.turn_controller._structured_type is Report
    assert h.turn_controller._output_type_dict is None


def test_build_accepts_object_rooted_dict(tmp_path):
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).with_output_type(schema).build()
    assert h.turn_controller._output_type_dict == schema
    # The dict was wrapped into pydantic-ai's StructuredDict type.
    assert h.turn_controller._structured_type is not None
    assert h.turn_controller._structured_type is not schema


def test_build_rejects_non_object_rooted_dict(tmp_path):
    with pytest.raises(BuilderError) as exc_info:
        (
            HarnessBuilder(workspace=tmp_path, model=TestModel())
            .with_output_type({"type": "array"})
            .build()
        )
    assert any("object-rooted" in p for p in exc_info.value.problems)


def test_build_rejects_malformed_json_schema(tmp_path):
    """Object-rooted but not a valid JSON Schema (typo'd `type`). Without the
    well-formedness check this builds fine and dies mid-turn with a raw
    jsonschema error, after the token spend."""
    schema = {"type": "object", "properties": {"a": {"type": "intger"}}}
    with pytest.raises(BuilderError) as exc_info:
        (HarnessBuilder(workspace=tmp_path, model=TestModel()).with_output_type(schema).build())
    assert any("malformed JSON Schema" in p for p in exc_info.value.problems)


def test_build_rejects_recursive_ref_schema(tmp_path):
    """A recursive `$defs` schema is well-formed JSON Schema but unsupported by
    pydantic-ai's StructuredDict — it must surface as a BuilderError, not a raw
    pydantic_ai UserError out of build()."""
    schema = {
        "type": "object",
        "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
        "properties": {"root": {"$ref": "#/$defs/Node"}},
    }
    with pytest.raises(BuilderError) as exc_info:
        (HarnessBuilder(workspace=tmp_path, model=TestModel()).with_output_type(schema).build())
    assert any("with_output_type" in p and "recursive" in p for p in exc_info.value.problems)


def test_build_rejects_other_types(tmp_path):
    with pytest.raises(BuilderError) as exc_info:
        (HarnessBuilder(workspace=tmp_path, model=TestModel()).with_output_type(int).build())
    assert any("with_output_type" in p for p in exc_info.value.problems)


def final_result(ctx: RunContext[Deps], note: str) -> str:
    """A custom tool that happens to carry pydantic-ai's output-tool name."""
    return note


def test_build_rejects_custom_tool_named_like_the_output_tool(tmp_path):
    """A structured harness registers pydantic-ai's `final_result` output tool
    on every run round, so a same-named custom tool built cleanly and then made
    EVERY turn raise UserError at the first request."""
    with pytest.raises(BuilderError) as exc_info:
        (
            HarnessBuilder(workspace=tmp_path, model=TestModel())
            .with_tool(final_result)
            .with_output_type(Report)
            .build()
        )
    assert any("final_result" in p and "output tool" in p for p in exc_info.value.problems), (
        exc_info.value.problems
    )


def test_build_allows_output_tool_name_without_output_type(tmp_path):
    """No structured output means no output tool exists — `final_result` is
    then an ordinary tool name and must not be rejected."""
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).with_tool(final_result).build()
    assert h.turn_controller._structured_type is None


def test_build_without_output_type_leaves_none(tmp_path):
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).build()
    assert h.turn_controller._structured_type is None
    assert h.turn_controller._output_type_dict is None
