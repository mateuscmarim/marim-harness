# Stringified Tool-Arg Coercion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a tool argument arrives as a JSON string but the tool's schema expects a structured (non-string) value at that position, decode it — recursively — before validation/dispatch, for both marim's own tools and MCP tools.

**Architecture:** One pure schema-walking primitive (`tools/coerce.py::coerce_by_schema`) drives the MCP path, invoked in the MCP `process_tool_call` hook against each tool's `inputSchema` (reached via a mutable holder the builder populates with the running server). marim's own tools keep the idiomatic pydantic `BeforeValidator` path, extended from top-level lists to list *elements* so a stringified object element also unwraps.

**Tech Stack:** Python 3.10+, Pydantic / Pydantic AI, pytest (+anyio), ruff, pyright.

## Global Constraints

- `requires-python >= 3.10` — no 3.11+ only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- Run order before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- Use `uv run …` for everything; never bare `python`/`pytest`/`pip`.
- Coverage gate is 90% (on by default). New modules need tests.
- Coercion must only ever *relax* acceptance: anything it can't confidently decode passes through unchanged so the downstream validator/server raises exactly today's error. Never change the JSON schema advertised to the model.

---

### Task 1: `coerce_by_schema` pure primitive

**Files:**
- Create: `src/marim_harness/tools/coerce.py`
- Test: `tests/test_coerce.py`

**Interfaces:**
- Produces: `coerce_by_schema(value: object, schema: dict, defs: dict | None = None) -> object` — returns `value` with stringified JSON decoded wherever `schema` expects a non-string type, recursing into decoded structures. Never raises.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_coerce.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_coerce.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.tools.coerce'`.

- [ ] **Step 3: Write the implementation**

Create `src/marim_harness/tools/coerce.py`:

```python
"""Schema-directed coercion of stringified tool arguments.

Some models serialize a structured tool argument as a JSON *string* — e.g. a
Playwright MCP tool's ``suites`` array arrives as ``'[{"title": …}]'`` rather than
a real array. :func:`coerce_by_schema` walks a JSON Schema alongside a value and,
*only* where the schema at that position expects a non-string type, decodes such a
string with ``json.loads`` and recurses into the result. A string where the schema
says ``string`` is left untouched; a string that won't parse is left untouched so
the downstream validator (own tools) or MCP server still raises the *real* error
instead of it being swallowed. Pure and side-effect-free — unit-tested directly.

This is the same "relax acceptance, never mask an error" contract as the
``LenientList`` before-validator in ``provider.py``; it just applies it by walking
a schema (which MCP tools expose as ``inputSchema``) rather than a pydantic type.
"""

from __future__ import annotations

import json

# JSON Schema types for which a bare string is worth trying to decode.
_NON_STRING = frozenset({"object", "array", "number", "integer", "boolean"})


def _maybe_decode(value: object) -> tuple[object, bool]:
    """``(json.loads(value), True)`` when ``value`` is a parseable JSON string, else
    ``(value, False)``."""
    if isinstance(value, str):
        try:
            return json.loads(value), True
        except (ValueError, TypeError):
            return value, False
    return value, False


def _schema_types(schema: dict) -> set[str]:
    """The declared ``type`` of a schema node as a set (JSON Schema allows a list),
    or an empty set when absent."""
    t = schema.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    return set()


def _resolve(schema: dict, defs: dict) -> dict:
    """Follow a local ``$ref`` (``#/$defs/Name`` or ``#/definitions/Name``) one or
    more hops; return ``schema`` unchanged when it has no resolvable ref."""
    seen: set[str] = set()
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or ref in seen:
            return schema
        seen.add(ref)
        target = defs.get(ref.rsplit("/", 1)[-1])
        if not isinstance(target, dict):
            return schema
        schema = target
    return schema


def coerce_by_schema(value: object, schema: object, defs: dict | None = None) -> object:
    """Return ``value`` with stringified JSON decoded wherever ``schema`` expects a
    non-string type, recursing into decoded structures. Never raises; an unknown,
    absent, or non-dict schema passes the value through unchanged."""
    if not isinstance(schema, dict):
        return value
    if defs is None:
        defs = {}
        for key in ("$defs", "definitions"):
            found = schema.get(key)
            if isinstance(found, dict):
                defs = {**defs, **found}
    schema = _resolve(schema, defs)

    # Combinators: a nullable/union schema. Use the first non-null branch — for the
    # ubiquitous ``[{...}, {"type": "null"}]`` pattern that is the real type.
    for combinator in ("anyOf", "oneOf"):
        branches = schema.get(combinator)
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, dict) and "null" not in _schema_types(branch):
                    return coerce_by_schema(value, branch, defs)
            return value

    types = _schema_types(schema)
    if types & _NON_STRING and "string" not in types:
        decoded, ok = _maybe_decode(value)
        if ok:
            value = decoded  # fall through and recurse into the decoded structure

    if isinstance(value, dict):
        props = schema.get("properties")
        addl = schema.get("additionalProperties")
        if not isinstance(props, dict) and not isinstance(addl, dict):
            return value
        result = {}
        for k, v in value.items():
            sub = props.get(k) if isinstance(props, dict) else None
            if sub is None and isinstance(addl, dict):
                sub = addl
            result[k] = coerce_by_schema(v, sub, defs) if isinstance(sub, dict) else v
        return result

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            return [coerce_by_schema(v, items, defs) for v in value]
        return value

    return value
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_coerce.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Lint & type-check the new module**

Run: `uv run ruff check src/marim_harness/tools/coerce.py tests/test_coerce.py && uv run pyright src/marim_harness/tools/coerce.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/tools/coerce.py tests/test_coerce.py
git commit -m "feat(tools): schema-directed coercion of stringified args"
```

---

### Task 2: Wire coercion into the MCP tool-call hook

**Files:**
- Modify: `src/marim_harness/mcp/config.py` (imports; `make_approval_hook` ~304-344; `build_mcp_servers` ~347-403)
- Test: `tests/test_mcp.py` (add near the `# --- approval hook ---` block, ~374-459)

**Interfaces:**
- Consumes: `coerce_by_schema` from Task 1.
- Produces: `make_approval_hook(label: str, trusted: bool, *, schema_holder: dict | None = None)` — new keyword-only `schema_holder`, a one-key dict the caller sets to `{"server": <MCPServer>}` after building the server. Backward compatible: omitted → no coercion.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp.py` (the file already imports `SimpleNamespace`, `Mode`, `make_approval_hook`, `pytest`, `Path`, and defines `_ctx` and `_runner`):

```python
@pytest.mark.anyio
async def test_hook_decodes_stringified_structured_arg(tmp_path: Path):
    """A model that stringifies a structured MCP arg gets it decoded from the tool's
    inputSchema before the server sees it."""
    calls: list = []
    tool = SimpleNamespace(
        name="save_plan",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "suites": {"type": "array", "items": {"type": "object"}},
            },
        },
    )

    async def list_tools():
        return [tool]

    holder = {"server": SimpleNamespace(list_tools=list_tools)}
    hook = make_approval_hook("pw", trusted=True, schema_holder=holder)
    args = {"name": "TodoMVC", "suites": '[{"title": "add"}]'}
    result = await hook(_ctx(Mode.auto), await _runner(calls), "save_plan", args)
    assert result == "RAN"
    assert calls == [("save_plan", {"name": "TodoMVC", "suites": [{"title": "add"}]})]


@pytest.mark.anyio
async def test_hook_without_holder_passes_args_through(tmp_path: Path):
    """No schema holder → no coercion (today's behavior)."""
    calls: list = []
    hook = make_approval_hook("pw", trusted=True)
    await hook(_ctx(Mode.auto), await _runner(calls), "save_plan", {"suites": "[1]"})
    assert calls == [("save_plan", {"suites": "[1]"})]


@pytest.mark.anyio
async def test_hook_schema_fetch_failure_falls_back(tmp_path: Path):
    """If the tool schema can't be fetched, dispatch uncoerced rather than erroring."""
    calls: list = []

    async def boom():
        raise RuntimeError("server down")

    holder = {"server": SimpleNamespace(list_tools=boom)}
    hook = make_approval_hook("pw", trusted=True, schema_holder=holder)
    await hook(_ctx(Mode.auto), await _runner(calls), "save_plan", {"suites": "[1]"})
    assert calls == [("save_plan", {"suites": "[1]"})]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_mcp.py -q -k "stringified or without_holder or fetch_failure"`
Expected: FAIL — `test_hook_decodes_stringified_structured_arg` fails (args not decoded); the other two may error on the unexpected `schema_holder` kwarg.

- [ ] **Step 3: Add the import**

In `src/marim_harness/mcp/config.py`, add to the imports near the top of the file (with the other `from ..` imports):

```python
from ..tools.coerce import coerce_by_schema
```

- [ ] **Step 4: Rewrite `make_approval_hook` to coerce args**

Replace the whole `make_approval_hook` function (currently `config.py:304-344`) with:

```python
def make_approval_hook(label: str, trusted: bool, *, schema_holder: dict | None = None):
    """Build a ``process_tool_call`` hook that gates an MCP server's tool calls by
    the live session mode: ``auto`` runs them, ``plan`` denies them (read-only),
    and ``ask`` runs a *trusted* server's calls but prompts for an *untrusted*
    one's via ``deps.request_approval``. A denied call returns a denial string,
    which the model receives as the tool result.

    ``label`` is the server's config name; it prefixes the tool name shown to the
    user so an approval prompt names which server is calling. The mode and the
    approval callback are read from ``ctx.deps`` at call time, so runtime mode
    switches take effect immediately.

    ``schema_holder`` (when given) is a one-key dict the caller populates with
    ``{"server": <MCPServer>}`` right after the server is built. On first dispatch
    the hook reads the server's tool input schemas — cheap, since pydantic-ai has
    already cached them via its own ``list_tools`` at startup — and decodes any
    argument a model stringified when the schema expects a structured value (see
    :func:`coerce_by_schema`). Absent a holder, a fetch error, or an unknown tool,
    the args pass through untouched, exactly as before."""
    schema_cache: dict[str, dict] = {}
    schema_state = {"loaded": False}

    async def _tool_schema(name: str) -> dict | None:
        if schema_holder is None:
            return None
        server = schema_holder.get("server")
        if server is None:
            return None
        if not schema_state["loaded"]:
            try:
                tools = await server.list_tools()
            except Exception:
                return None  # server hiccup — fall back to uncoerced dispatch
            for tool in tools:
                nm = getattr(tool, "name", None)
                sch = getattr(tool, "inputSchema", None)
                if isinstance(nm, str) and isinstance(sch, dict):
                    schema_cache[nm] = sch
            schema_state["loaded"] = True
        return schema_cache.get(name)

    async def _coerce_args(name: str, args):
        if isinstance(args, dict):
            schema = await _tool_schema(name)
            if isinstance(schema, dict):
                return coerce_by_schema(args, schema)
        return args

    async def hook(ctx, call_tool, name, args):
        deps = ctx.deps
        display = f"{label}_{name}"
        # Read the workspace root at call time, like mode/request_approval below,
        # so a large result can be offloaded under the project rather than inline.
        ws = getattr(deps, "workspace", None)
        root = getattr(ws, "root", None) if ws is not None else None
        mode = getattr(ws, "mode", None) if ws is not None else None
        if mode is Mode.plan:
            return f"Denied: {display} is blocked in read-only plan mode."
        # Decode any stringified structured arg before the server (and the approval
        # prompt) ever see it, so a model that serialized a nested value as a string
        # doesn't burn a turn on the server's rejection.
        args = await _coerce_args(name, args)
        if mode is Mode.auto or trusted:
            result = await call_tool(name, args)
            return _bound_tool_result(
                result, label=label, name=name, args=args, workspace_root=root
            )
        # ask mode against an untrusted server: prompt the user.
        ui = getattr(deps, "ui", None)
        approve = getattr(ui, "request_approval", None) if ui is not None else None
        if approve is None:
            return f"Denied: {display} needs approval but none is available here."
        decision = await approve(_McpApprovalCall(display, args or {}))
        if decision is True:
            result = await call_tool(name, args)
            return _bound_tool_result(
                result, label=label, name=name, args=args, workspace_root=root
            )
        return f"Denied: the user rejected {display}."

    return hook
```

- [ ] **Step 5: Populate the holder in `build_mcp_servers`**

In `build_mcp_servers` (`config.py:369-402`), inside the `for name, spec in specs.items():` loop, replace the hook creation and server construction so the holder is created, passed, and filled. Change:

```python
            hook = make_approval_hook(name, bool(spec.get("trust", False)))
            if "command" in spec:
                server = _QuietStdioServer(
                    ...
                )
            elif "url" in spec:
                ...
                server = kind(
                    ...
                )
            else:
                notes.append(
                    f"MCP server {name!r}: needs 'command' or 'url'; skipped."
                )
                continue
            servers.append(server)
```

to:

```python
            holder: dict = {}
            hook = make_approval_hook(
                name, bool(spec.get("trust", False)), schema_holder=holder
            )
            if "command" in spec:
                server = _QuietStdioServer(
                    command=spec["command"],
                    args=list(spec.get("args", [])),
                    env=spec.get("env"),
                    cwd=spec.get("cwd"),
                    tool_prefix=name,
                    process_tool_call=hook,
                    max_retries=_MCP_TOOL_RETRIES,
                )
            elif "url" in spec:
                kind = (
                    MCPServerSSE
                    if spec.get("type") == "sse"
                    else MCPServerStreamableHTTP
                )
                server = kind(
                    url=spec["url"],
                    headers=spec.get("headers"),
                    tool_prefix=name,
                    process_tool_call=hook,
                    max_retries=_MCP_TOOL_RETRIES,
                )
            else:
                notes.append(
                    f"MCP server {name!r}: needs 'command' or 'url'; skipped."
                )
                continue
            # The hook needs the server to read tool inputSchemas, but the server
            # needs the hook at construction — so hand the hook a holder now and
            # fill it once the server exists.
            holder["server"] = server
            servers.append(server)
```

- [ ] **Step 6: Run the new + existing hook tests**

Run: `uv run pytest --no-cov tests/test_mcp.py -q`
Expected: PASS (all existing approval-hook tests still green, three new ones pass).

- [ ] **Step 7: Lint & type-check**

Run: `uv run ruff check src/marim_harness/mcp/config.py tests/test_mcp.py && uv run pyright src/marim_harness/mcp/config.py`
Expected: no errors. (Note: `except Exception` is intentional fallback breadth — if ruff `BLE001` flags it, add `# noqa: BLE001` with a short reason, matching how the codebase handles best-effort catches elsewhere.)

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/mcp/config.py tests/test_mcp.py
git commit -m "feat(mcp): decode stringified structured args via inputSchema"
```

---

### Task 3: Extend own-tool coercion to list elements

**Files:**
- Modify: `src/marim_harness/tools/provider.py` (`_decode_json_list` ~41-52; `LenientList` ~60; `update_tasks` ~357; `ask_user` ~374)
- Test: `tests/test_provider.py` (extend the block at ~634-660)

**Interfaces:**
- Consumes: nothing new.
- Produces: `Lenient = Annotated[_T, BeforeValidator(_decode_json)]` — unwrap a stringified JSON value for a single (non-list) tool arg or list element. `_decode_json_list` is renamed to `_decode_json` (neutral name; same body) and reused by both `LenientList` and `Lenient`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_provider.py`, right after `test_array_arg_accepts_json_stringified_list` (~660):

```python
@pytest.mark.parametrize(
    ("tool_name", "param", "extra", "elements"),
    [
        ("update_tasks", "todos", {}, ['{"text": "do it", "status": "pending"}']),
        (
            "ask_user",
            "questions",
            {},
            ['{"question": "ok?", "header": "h", "options": [{"label": "yes"}]}'],
        ),
    ],
)
def test_array_arg_accepts_stringified_object_elements(tool_name, param, extra, elements):
    """Beyond a whole stringified list, a model may stringify each *element* object.
    The element-level before-validator must unwrap those too, and the advertised
    schema stays an array of objects."""
    agent = _build_agent()
    schema = agent._function_toolset.tools[tool_name].function_schema
    assert schema.json_schema["properties"][param]["type"] == "array"
    out = schema.validator.validate_python({param: elements, **extra})
    assert isinstance(out[param], list) and out[param]
    # each element decoded into the real dataclass, not left as a str
    assert not any(isinstance(item, str) for item in out[param])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_provider.py -q -k stringified_object_elements`
Expected: FAIL — a `ValidationError` (a `str` element can't validate as `Task`/`Question`).

- [ ] **Step 3: Rename the decoder and add `Lenient`**

In `src/marim_harness/tools/provider.py`, replace `_decode_json_list` and the `LenientList` definition (`41-60`) with:

```python
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
```

- [ ] **Step 4: Apply `Lenient` to the object element types**

In `update_tasks` (`provider.py:357`), change the signature param from:

```python
async def update_tasks(ctx: RunContext[Deps], todos: LenientList[Task]) -> str:
```

to:

```python
async def update_tasks(ctx: RunContext[Deps], todos: LenientList[Lenient[Task]]) -> str:
```

In `ask_user` (`provider.py:374`), change:

```python
async def ask_user(ctx: RunContext[Deps], questions: LenientList[Question]) -> str:
```

to:

```python
async def ask_user(ctx: RunContext[Deps], questions: LenientList[Lenient[Question]]) -> str:
```

Leave `present_plan`'s `steps: LenientList[str]` unchanged — its elements are plain strings, not objects.

- [ ] **Step 5: Run the new + existing provider tests for this area**

Run: `uv run pytest --no-cov tests/test_provider.py -q -k "stringified or array_arg"`
Expected: PASS — the new element test passes and the existing whole-list / real-list / malformed-string tests stay green.

- [ ] **Step 6: Lint & type-check**

Run: `uv run ruff check src/marim_harness/tools/provider.py tests/test_provider.py && uv run pyright src/marim_harness/tools/provider.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/tools/provider.py tests/test_provider.py
git commit -m "feat(tools): unwrap stringified object elements in list args"
```

---

### Task 4: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full CI-equivalent gate**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: ruff clean, pyright clean, all tests pass, coverage ≥ 90%.

- [ ] **Step 2: If coverage dips below 90% on the new modules**

Inspect the coverage report for `tools/coerce.py` and add a targeted unit test for any uncovered branch (e.g. `oneOf`, `additionalProperties`, `$ref` cycle guard). Re-run Step 1.

- [ ] **Step 3: Commit any added coverage tests**

```bash
git add tests/test_coerce.py
git commit -m "test(coerce): cover remaining schema branches"
```

---

## Self-Review

**Spec coverage:**
- Shared `coerce_by_schema` primitive → Task 1. ✓
- MCP wiring via holder + cached `list_tools()` schema, uncoerced fallback → Task 2. ✓
- Own-tools before-validator idiom extended to nested/element objects → Task 3. ✓
- Error handling (relax-only, no new failure modes) → enforced by fallbacks in Tasks 1–3 and asserted by `test_hook_schema_fetch_failure_falls_back`, `test_leaves_unparseable_string_untouched`. ✓
- Tests for `coerce_by_schema` (nested object, array-of-objects, `$ref`, nullable `anyOf`, non-JSON left alone, genuine-string left alone, identity) → Task 1. ✓
- MCP hook test (decoded before `call_tool`; unfetchable → uncoerced) → Task 2. ✓
- Own-tools nested-object test + schema-unchanged assertion → Task 3. ✓

**Known scope boundary (deliberate, not a gap):** own-tool coercion covers the whole-list and per-element-object cases (`Task`, `Question`). Deeper field nesting *inside* an own-tool object (e.g. a stringified `Question.options` list) is not separately unwrapped — the own-tool object schemas are shallow and this case is unobserved; the MCP path (`coerce_by_schema`) does recurse arbitrarily deep. If a real case appears, apply `Lenient`/`LenientList` to that field.

**Placeholder scan:** no TBD/TODO; every code step shows complete code. ✓

**Type consistency:** `coerce_by_schema(value, schema, defs=None)` used identically in Task 1 and Task 2. `_decode_json` (renamed) referenced by both `LenientList` and `Lenient` in Task 3. `schema_holder` keyword matches between `make_approval_hook` (Task 2 Step 4) and `build_mcp_servers` (Task 2 Step 5). ✓
