# Schema-directed coercion of stringified tool arguments

## Problem

Models sometimes serialize a structured tool argument as a JSON **string** instead of
emitting a real JSON object/array. Example (observed live): a Playwright MCP tool's
`suites` parameter expects an array of objects, but the model passed
`'[{"name": …}]'` — a string. The MCP server rejects it, the model burns a turn
self-correcting ("I passed it incorrectly … let me try again with the correct format").

marim already softens this in **one narrow place**: `LenientList`
(`tools/provider.py:41`) is a pydantic `BeforeValidator` that `json.loads`-es a string
that should be a top-level list, used on `update_tasks`, `ask_user`, `present_plan`.
Two gaps remain:

1. **MCP tools** get no coercion at all — `make_approval_hook` (`mcp/config.py:316`)
   forwards `args` verbatim to the server (`config.py:327,338`). This is the case in
   the screenshot.
2. **marim's own tools** only unwrap *top-level lists*. A stringified nested object,
   or a stringified value that isn't a list, still fails.

## Goal

When a tool argument arrives as a JSON string but the tool's schema expects a
non-string type at that position, decode it before validation/dispatch — recursively,
so nested stringified structures are also unwrapped. Applies to **both** marim's own
tools and MCP tools. Never corrupt a genuine string field; never mask a real
validation error.

Non-goals: fixing malformed (non-JSON) strings, guessing types without a schema,
changing the schema advertised to the model, or altering the existing
`_drop_nameless_tool_calls` repair (`controller.py`), which stays as the last-resort
"delete structurally broken calls" pass.

## Design

### Shared primitive — `tools/coerce.py` (new leaf module)

A pure, side-effect-free function, unit-tested directly:

```python
def coerce_by_schema(value: object, schema: dict) -> object
```

Walks a JSON Schema alongside `value`. **Only** where the schema at the current
position expects a non-string type (`object`, `array`, `number`, `integer`,
`boolean`) and `value` is a `str`, it attempts `json.loads`; on success it recurses
into the decoded value, on failure it returns the string untouched. A `string`-typed
position is never touched. Unknown/absent schema at a position → pass through
unchanged.

Schema shapes handled:
- `type: object` with `properties` → recurse per key; also `additionalProperties` schema.
- `type: array` with `items` → recurse per element.
- `$ref` / `$defs` (local refs) → resolve then recurse.
- `anyOf` / `oneOf` (the nullable `[{type: object}, {type: null}]` wrappers MCP
  servers emit) → pick the first non-null branch whose coercion changes/validates the
  value; otherwise pass through.
- Missing/`true`/unknown schema → identity.

The rule matches the existing `LenientList` contract: relax acceptance, never swallow
a real error. Worst case, `coerce_by_schema` returns the input unchanged and the
downstream validator/server produces exactly today's error.

### Wiring — MCP tools

In `make_approval_hook` (`mcp/config.py`), before each `call_tool(name, args)`
dispatch, run `args = coerce_by_schema(args, input_schema)` where `input_schema` is
looked up by tool `name` from a **cached** `{tool_name: inputSchema}` map.

Schema source: `server.list_tools()` (`pydantic_ai/mcp.py:1010`) returns each tool's
`inputSchema`. The hook closure is built (`config.py:373`) before `server` exists
(`config.py:375`), so `make_approval_hook` takes a small **mutable holder**;
`build_mcp_servers` assigns `holder.server = server` immediately after construction.
On first call the hook fetches + caches the tool→schema map from the holder's server.

Fallback: if the schema can't be fetched (holder unset, `list_tools` errors, tool
absent), dispatch **uncoerced** — identical to today's behavior, so no regression and
no new failure mode. `name` in the hook is the server-side (unprefixed) tool name,
matching `list_tools()` keys.

### Wiring — marim's own tools

Keep the idiomatic pydantic path rather than routing raw args through
`coerce_by_schema`. Generalize the existing before-validator idiom:
- `LenientList` stays for list params.
- Add a lenient object/model coercion (a model-level `BeforeValidator` that
  `json.loads`-es a stringified dict) applied to the nested models used by own tools,
  so pydantic's own recursion carries unwrapping into nested fields.

This realizes the same rule as `coerce_by_schema` for own tools without hand-walking
JSON Schema, and preserves the schema advertised to the model.

## Error handling

Coercion only *relaxes* acceptance. Anything it cannot confidently decode is passed
through unchanged to the existing validator (own tools) or MCP server (MCP tools),
which still raises the same `ModelRetry` / server error as today. No new exceptions
are introduced by the coercion layer.

## Testing

- **Unit — `coerce_by_schema`** (`tests/test_coerce.py`, new): nested object;
  array-of-objects (the `suites` case); `$ref`/`$defs`; nullable `anyOf`; non-JSON
  string left alone; genuine `string`-typed field containing JSON-looking text left
  alone; missing schema = identity.
- **MCP hook** (`tests/test_mcp*.py`): a stringified arg is decoded before
  `call_tool` receives it; unfetchable schema → dispatched uncoerced.
- **Own tools** (extend `tests/test_provider.py:652`): a stringified *nested object*
  param now validates, and the advertised JSON schema is unchanged.

## Files touched

- `src/marim_harness/tools/coerce.py` (new)
- `src/marim_harness/mcp/config.py` (holder + hook coercion + cache)
- `src/marim_harness/tools/provider.py` (lenient object idiom on nested models)
- `tests/test_coerce.py` (new), `tests/test_provider.py`, MCP hook test
