"""Codex JSON Pointer compatibility against a captured public MCP tool schema."""

import json
from copy import deepcopy
from pathlib import Path

import httpx2
import pytest
from jsonschema import Draft7Validator, Draft202012Validator
from pydantic_ai import Agent, Tool
from pydantic_ai.exceptions import UserError
from pydantic_ai.profiles.openai import OpenAIJsonSchemaTransformer

from tests._codex_subscription import source, sse
from tests._codex_subscription import wire as wire  # noqa: F401


def wiki_schema():
    return json.loads(
        (Path(__file__).parent / "fixtures/mddocs_set_wiki_index_schema.json").read_text()
    )


def transform(schema):
    cls = source().build("gpt-6-astra").profile["json_schema_transformer"]
    return cls(schema, strict=False).walk()


@pytest.mark.anyio
@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-terra"])
@pytest.mark.parametrize("deferred", [False, True])
async def test_wiki_schema_wire(wire, model, deferred):
    schema = wiki_schema()
    original = deepcopy(schema)

    def never_called(**kwargs):
        raise AssertionError("This schema probe must not execute an MCP mutation")

    tool = Tool.from_schema(never_called, "mddocs_set_wiki_index", "Set wiki index", schema)
    tool.defer_loading = deferred
    agent = Agent(source().build(model), tools=[tool])

    async def respond(request):
        body = json.loads(await request.aread())
        definition = next(t for t in body["tools"] if t.get("name") == "mddocs_set_wiki_index")
        parameters = definition["parameters"]
        ref = parameters["properties"]["home_document_id"]["anyOf"][0]["$ref"]
        if not ref.startswith("#/$defs/") or "/" in ref[len("#/$defs/") :]:
            return httpx2.Response(
                400,
                json={
                    "error": {
                        "message": "reference must target top-level definition",
                        "type": "invalid_request_error",
                    }
                },
            )
        assert parameters["$defs"][ref.removeprefix("#/$defs/")] == {
            "type": "string",
            "pattern": "^[a-z0-9]{8}$",
        }
        assert definition.get("defer_loading", False) is deferred
        assert any(t["type"] == "tool_search" for t in body["tools"]) is deferred
        return httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, content=sse())

    wire.handler = respond
    assert (await agent.run("Reply done without calling tools")).output == "done"
    assert len(wire.requests) == 1
    assert schema == original


@pytest.mark.parametrize(
    "value,valid",
    [(None, True), ("abcd1234", True), ("short", False), ("ABCD1234", False), (42, False)],
)
def test_schema_semantics(wire, value, valid):
    schema = wiki_schema()
    original = deepcopy(schema)
    normalized = transform(schema)
    instance = dict(wiki_id="abcd1234", expected_version=0, nodes=[], home_document_id=value)
    assert Draft7Validator(schema).is_valid(instance) is valid
    assert Draft7Validator(normalized).is_valid(instance) is valid
    assert schema == original
    assert transform(schema) == normalized


def test_reference_siblings(wire):
    schema = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "pattern": "^[a-z0-9]{8}$"},
            "value": {
                "$ref": "#/properties/target",
                "pattern": "^a",
                "description": "Starts with a",
            },
        },
    }
    normalized = transform(schema)
    for value, valid in [("abcd1234", True), ("zbcd1234", False), ("a", False), (42, False)]:
        assert Draft202012Validator(schema).is_valid({"value": value}) is valid
        assert Draft202012Validator(normalized).is_valid({"value": value}) is valid
    assert normalized["properties"]["value"]["description"] == "Starts with a"


def test_pointer_chains(wire):
    schema = {
        "type": "object",
        "$defs": {"marim_local_ref_1": {"type": "boolean"}},
        "properties": {
            "a/b~c": {"anyOf": [{"type": "string", "pattern": "^ok$"}, {"type": "null"}]},
            "middle": {"$ref": "#/properties/a~1b~0c/anyOf/0"},
            "value": {"$ref": "#/properties/middle"},
        },
    }
    normalized = transform(schema)
    assert normalized["$defs"]["marim_local_ref_1"] == {"type": "boolean"}
    assert normalized["properties"]["value"]["$ref"].startswith("#/$defs/")
    assert normalized["properties"]["middle"]["$ref"].startswith("#/$defs/")
    assert Draft7Validator(normalized).is_valid({"value": "ok"})
    assert not Draft7Validator(normalized).is_valid({"value": "bad"})


def test_pointer_cycle(wire):
    schema = {
        "type": "object",
        "properties": {
            "node": {
                "type": "object",
                "properties": {
                    "next": {
                        "anyOf": [
                            {"$ref": "#/properties/node"},
                            {"type": "null"},
                        ]
                    }
                },
            }
        },
    }
    original = deepcopy(schema)
    normalized = transform(schema)
    assert len(normalized["$defs"]) == 1
    assert Draft7Validator(normalized).is_valid({"node": {"next": {"next": None}}})
    assert not Draft7Validator(normalized).is_valid({"node": {"next": "invalid"}})
    assert schema == original


@pytest.mark.parametrize("ref", ["#", "#/$defs/Node"])
def test_supported_recursion(wire, ref):
    schema = {
        "type": "object",
        "properties": {"next": {"anyOf": [{"$ref": ref}, {"type": "null"}]}},
    }
    if ref != "#":
        schema = {"$defs": {"Node": schema}, "$ref": ref}
    assert transform(schema) == OpenAIJsonSchemaTransformer(schema, strict=False).walk()


@pytest.mark.parametrize(
    "ref", ["#/missing", "#/properties/value/type", "#/properties/value/anyOf/99"]
)
def test_invalid_pointer(wire, ref):
    schema = {
        "type": "object",
        "properties": {
            "value": {"type": "string", "anyOf": [{"type": "string"}]},
            "other": {"$ref": ref},
        },
    }
    with pytest.raises(UserError, match="Cannot normalize local JSON Schema reference"):
        transform(schema)
    assert wire.requests == []


def test_reference_data(wire):
    data = {"$ref": "#/not-a-schema-pointer"}
    schema = {
        "type": "object",
        "properties": {
            "value": {"type": "object", "default": data, "examples": [data], "enum": [data]},
            "external": {"$ref": "https://example.invalid/schema.json"},
        },
    }
    original = deepcopy(schema)
    assert transform(schema) == OpenAIJsonSchemaTransformer(schema, strict=False).walk()
    assert schema == original
    assert wire.requests == []
