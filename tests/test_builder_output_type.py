import pytest
from pydantic import BaseModel
from pydantic_ai.models.test import TestModel

from marim_harness import BuilderError, HarnessBuilder


class Report(BaseModel):
    summary: str


def test_build_accepts_basemodel_schema(tmp_path):
    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_output_type(Report)
         .build())
    # Harness does not retain its config; the TurnController is where the
    # schema lands (Task 4 reads it).
    assert h.turn_controller._structured_type is Report
    assert h.turn_controller._output_type_dict is None


def test_build_accepts_object_rooted_dict(tmp_path):
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_output_type(schema)
         .build())
    assert h.turn_controller._output_type_dict == schema
    # The dict was wrapped into pydantic-ai's StructuredDict type.
    assert h.turn_controller._structured_type is not None
    assert h.turn_controller._structured_type is not schema


def test_build_rejects_non_object_rooted_dict(tmp_path):
    with pytest.raises(BuilderError) as exc_info:
        (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_output_type({"type": "array"})
         .build())
    assert any("object-rooted" in p for p in exc_info.value.problems)


def test_build_rejects_other_types(tmp_path):
    with pytest.raises(BuilderError) as exc_info:
        (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_output_type(int)
         .build())
    assert any("with_output_type" in p for p in exc_info.value.problems)


def test_build_without_output_type_leaves_none(tmp_path):
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).build()
    assert h.turn_controller._structured_type is None
    assert h.turn_controller._output_type_dict is None
