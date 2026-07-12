"""Schema'd spawn output: the native StructuredDict path vs the
prompt-contract fallback (claude-cli backends, non-object schema roots)."""

import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.subagents.output_schema import output_contract, resolve_output_schema
from tests.conftest import _make_deps, _make_harness

FINDINGS = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
    "required": ["findings"],
}


def test_output_contract_embeds_the_schema_and_demands_bare_json():
    text = output_contract(FINDINGS)
    assert "ONLY a JSON object" in text
    assert '"findings"' in text


def test_resolve_passes_object_schemas_to_structured_output():
    assert resolve_output_schema(FINDINGS, None) == (FINDINGS, "")


def test_resolve_falls_back_for_the_cli_backend():
    schema, contract = resolve_output_schema(FINDINGS, "claude-cli")
    assert schema is None
    assert "Output contract" in contract and '"findings"' in contract


def test_resolve_falls_back_for_non_object_roots():
    array_root = {"type": "array", "items": {"type": "string"}}
    schema, contract = resolve_output_schema(array_root, None)
    assert schema is None
    assert "Output contract" in contract


def test_resolve_no_schema_is_a_no_op():
    assert resolve_output_schema(None, "claude-cli") == (None, "")
    assert resolve_output_schema(None, None) == (None, "")


@pytest.mark.anyio
async def test_object_schema_rides_structured_output(tmp_path: Path):
    h = _make_harness(
        TestModel(call_tools=[], custom_output_args={"findings": ["bug in x"]}),
        _make_deps(tmp_path),
    )
    out = await h.subagents.run("explore", "review", "s1", output_schema=FINDINGS)
    assert json.loads(out) == {"findings": ["bug in x"]}


@pytest.mark.anyio
async def test_non_object_schema_falls_back_to_prompt_contract(tmp_path: Path):
    seen = {}

    def fn(messages, info):
        seen["prompt"] = messages[0].parts[-1].content
        return ModelResponse(parts=[TextPart(content="plain text")])

    h = _make_harness(FunctionModel(fn), _make_deps(tmp_path))
    out = await h.subagents.run(
        "explore", "list things", "s1",
        output_schema={"type": "array", "items": {"type": "string"}},
    )
    assert "Output contract" in seen["prompt"]
    assert "plain text" in out


@pytest.mark.anyio
async def test_no_schema_leaves_the_spawn_unchanged(tmp_path: Path):
    seen = {}

    def fn(messages, info):
        seen["prompt"] = messages[0].parts[-1].content
        return ModelResponse(parts=[TextPart(content="ok")])

    h = _make_harness(FunctionModel(fn), _make_deps(tmp_path))
    out = await h.subagents.run("explore", "just look", "s1")
    assert "Output contract" not in seen["prompt"]
    assert "ok" in out
