# Claude Code CLI Sub-Agent Backend — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an authored sub-agent declare `backend: claude-cli` and, when spawned, run the Claude Code CLI (`claude -p`) in headless stream-json mode instead of the in-process Pydantic AI loop — streaming its activity to the UI and returning its final report through the existing spawn contract.

**Architecture:** A new leaf module `subagents_cli.py` holds the backend (pure helpers + a thin `ClaudeCliRunner` that spawns the process and translates its `stream-json` events into the Pydantic AI events the TUI already renders). `AgentDef` gains `backend`/`model` frontmatter fields. `SubagentRunner._execute_spawn` gets one early branch that routes CLI agents to a parallel wrapper, leaving the native path byte-for-byte unchanged.

**Tech Stack:** Python 3.10+, asyncio subprocess, Pydantic AI 1.107.0 (`pydantic_ai.messages`, `pydantic_ai.result.RunUsage`), pytest + anyio.

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax (e.g. no `X | Y` in non-annotation runtime positions that 3.10 rejects; annotations are fine via existing `from __future__ import annotations` usage).
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pytest`/`pip`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- CI order is ruff → pyright → pytest; match locally before claiming done.
- Tool docstrings are model-facing product copy — write them as such.
- Pure helpers stay side-effect-free and are unit-tested directly; I/O wiring is the thin layer.
- Claude Code tool-name mapping (verbatim): `read_file→Read, glob→Glob, grep→Grep, web_search→WebSearch, fetch_url→WebFetch, write_file→Write, edit_file→Edit, bash→Bash`. `tree` and the LSP tools have **no** Claude Code equivalent and are dropped.
- Permission mode mapping (verbatim): auto mode → `acceptEdits`; ask/plan mode → `plan`.
- Binary resolution: `$MARIM_CLAUDE_CLI_BIN` if set, else `claude` on PATH.
- Model precedence (first hit wins): per-spawn `model` arg → `defn.model` frontmatter → `$MARIM_CLAUDE_CLI_MODEL` → omit `--model`.

---

## File Structure

- **Create** `src/marim_harness/subagents_cli.py` — backend module: exceptions (`CliUnavailable`, `CliRunError`), pure helpers (`resolve_cli_binary`, `cli_permission_mode`, `map_tools_to_cc`, `build_cli_argv`, `synth_usage`), `CliResult` dataclass, `CliStreamTranslator`, `ClaudeCliRunner`.
- **Modify** `src/marim_harness/workspace/agents.py` — add `backend`/`model` fields to `AgentDef`; read them in `_parse_agent`; built-ins keep defaults.
- **Modify** `src/marim_harness/subagents.py` — add `import os`; insert the CLI branch in `_execute_spawn`; add `_execute_cli_spawn`, `_cli_mcp_note`, `_run_cli`.
- **Modify** `src/marim_harness/tools/provider.py` — one note in the `spawn_agent` docstring about CLI model aliases (model-facing).
- **Create** `tests/test_subagents_cli.py` — unit tests for helpers + translator + a fake-binary `ClaudeCliRunner` integration test.
- **Create** `tests/test_agent_backend_field.py` — frontmatter parse tests for `backend`/`model`.
- **Create** `tests/test_subagent_cli_spawn.py` — end-to-end `SubagentRunner.run` over a fake binary.

---

## Task 1: `AgentDef` gains `backend` and `model` frontmatter fields

**Files:**
- Modify: `src/marim_harness/workspace/agents.py:47-63` (dataclass), `:132-139` (`_parse_agent` return)
- Test: `tests/test_agent_backend_field.py`

**Interfaces:**
- Produces: `AgentDef.backend: str` (default `"native"`), `AgentDef.model: str | None` (default `None`). Read from frontmatter keys `backend:` and `model:`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_backend_field.py
from pathlib import Path

from marim_harness.workspace.agents import find_agent


def _write_agent(tmp_path: Path, name: str, frontmatter: str, body: str = "Do work.") -> None:
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def test_backend_and_model_parsed_from_frontmatter(tmp_path: Path):
    _write_agent(
        tmp_path, "cli-worker",
        "description: CLI worker\nbackend: claude-cli\nmodel: opus\ntools: read_file, edit_file",
    )
    defn = find_agent(tmp_path, "cli-worker")
    assert defn is not None
    assert defn.backend == "claude-cli"
    assert defn.model == "opus"


def test_backend_defaults_to_native(tmp_path: Path):
    _write_agent(tmp_path, "plain", "description: Plain agent")
    defn = find_agent(tmp_path, "plain")
    assert defn is not None
    assert defn.backend == "native"
    assert defn.model is None


def test_builtins_are_native(tmp_path: Path):
    defn = find_agent(tmp_path, "explore")
    assert defn is not None and defn.backend == "native" and defn.model is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_agent_backend_field.py -v`
Expected: FAIL — `AgentDef.__init__() got an unexpected keyword argument` is not raised yet because the attributes don't exist; assertions on `defn.backend` fail with `AttributeError`.

- [ ] **Step 3: Add the dataclass fields**

In `src/marim_harness/workspace/agents.py`, extend the `AgentDef` dataclass (keep existing fields and docstring; add the two new fields after `plugin`):

```python
@dataclass(frozen=True)
class AgentDef:
    name: str
    description: str
    prompt: str
    tools: frozenset[str]
    source: str
    plugin: str | None = None
    # Which runner executes this agent. "native" is the in-process Pydantic AI
    # loop; "claude-cli" spawns the Claude Code CLI (see subagents_cli.py). New
    # backends slot in here without touching discovery.
    backend: str = "native"
    # Backend-specific default model. For "claude-cli" this is a Claude Code model
    # name (e.g. "opus"/"sonnet" alias or a full id), passed verbatim to --model;
    # ignored by the native backend, which tracks the harness's runtime model.
    model: str | None = None
```

- [ ] **Step 4: Read the fields in `_parse_agent`**

In the `return AgentDef(...)` block (`:132-139`), add the two reads (frontmatter `data` is already parsed above):

```python
    backend_raw = data.get("backend")
    backend = backend_raw.strip() if isinstance(backend_raw, str) and backend_raw.strip() else "native"
    model_raw = data.get("model")
    model = model_raw.strip() if isinstance(model_raw, str) and model_raw.strip() else None
    return AgentDef(
        name=name,
        description=description.strip(),
        prompt=match.group(2).strip(),
        tools=_parse_tools(data.get("tools")),
        source=source,
        plugin=plugin,
        backend=backend,
        model=model,
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_agent_backend_field.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/workspace/agents.py tests/test_agent_backend_field.py
git commit -m "feat(agents): add backend and model frontmatter fields to AgentDef"
```

---

## Task 2: Pure CLI helpers (binary, permission mode, tool map, argv, usage)

**Files:**
- Create: `src/marim_harness/subagents_cli.py`
- Test: `tests/test_subagents_cli.py`

**Interfaces:**
- Produces:
  - `resolve_cli_binary() -> str | None`
  - `cli_permission_mode(allow_gated: bool) -> str`
  - `map_tools_to_cc(tool_names) -> list[str]`
  - `build_cli_argv(binary, prompt, system_prompt, permission_mode, allowed_tools, model) -> list[str]`
  - `synth_usage(cli_usage: dict | None, num_turns: int) -> RunUsage`
  - `CliResult(output: str, usage: RunUsage)` dataclass
  - `CliUnavailable(Exception)`, `CliRunError(Exception)`
  - module constant `CLI_BINARY_ENV = "MARIM_CLAUDE_CLI_BIN"`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_subagents_cli.py
from pydantic_ai.result import RunUsage

from marim_harness.subagents_cli import (
    CLI_BINARY_ENV,
    build_cli_argv,
    cli_permission_mode,
    map_tools_to_cc,
    resolve_cli_binary,
    synth_usage,
)
from marim_harness.tools.names import READ_TOOLS, SUBAGENT_TOOLS


def test_permission_mode_maps_to_auto_and_plan():
    assert cli_permission_mode(True) == "acceptEdits"
    assert cli_permission_mode(False) == "plan"


def test_tool_map_drops_unmapped_and_sorts():
    # READ_TOOLS = read_file, glob, tree, grep + LSP tools. Only read_file/glob/grep
    # map; tree and LSP names are dropped.
    assert map_tools_to_cc(READ_TOOLS) == ["Glob", "Grep", "Read"]
    assert map_tools_to_cc(SUBAGENT_TOOLS) == [
        "Bash", "Edit", "Glob", "Grep", "Read", "WebFetch", "WebSearch", "Write",
    ]


def test_build_argv_includes_required_flags():
    argv = build_cli_argv(
        "/usr/bin/claude", "do the task", "You are a worker.",
        "acceptEdits", ["Read", "Edit"], "opus",
    )
    assert argv[:3] == ["/usr/bin/claude", "-p", "do the task"]
    assert "--output-format" in argv and "stream-json" in argv and "--verbose" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "You are a worker."
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Edit"
    assert argv[argv.index("--model") + 1] == "opus"


def test_build_argv_omits_model_and_tools_when_absent():
    argv = build_cli_argv("claude", "t", "s", "plan", [], None)
    assert "--model" not in argv
    assert "--allowedTools" not in argv


def test_resolve_binary_prefers_env(monkeypatch, tmp_path):
    fake = tmp_path / "myclaude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv(CLI_BINARY_ENV, str(fake))
    assert resolve_cli_binary() == str(fake)


def test_resolve_binary_none_when_missing(monkeypatch):
    monkeypatch.setenv(CLI_BINARY_ENV, "definitely-not-a-real-binary-xyz")
    assert resolve_cli_binary() is None


def test_synth_usage_maps_token_fields():
    u = synth_usage(
        {"input_tokens": 10, "output_tokens": 5,
         "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1},
        num_turns=3,
    )
    assert isinstance(u, RunUsage)
    assert u.input_tokens == 10 and u.output_tokens == 5
    assert u.cache_read_tokens == 2 and u.cache_write_tokens == 1
    assert u.requests == 3


def test_synth_usage_tolerates_none():
    u = synth_usage(None, num_turns=0)
    assert u.input_tokens == 0 and u.output_tokens == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagents_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.subagents_cli'`.

- [ ] **Step 3: Create the module with helpers**

```python
# src/marim_harness/subagents_cli.py
"""Run the Claude Code CLI (`claude -p`) as a sub-agent backend.

An authored agent with `backend: claude-cli` is spawned as an external `claude`
process in headless stream-json mode instead of the in-process Pydantic AI loop.
This module is backend-only: the pure translation helpers (binary resolve, argv
build, harness→Claude-Code tool-name mapping, permission-mode selection, usage
synthesis), the stream-event translator, and the thin `ClaudeCliRunner` that
spawns the process and forwards its activity to the UI. The harness wrapping
(worktree, hooks bracketing, output cap, background persist) stays in
`subagents.py`, so this module is unit-tested without the rest of the harness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.result import RunUsage

logger = logging.getLogger(__name__)

CLI_BINARY_ENV = "MARIM_CLAUDE_CLI_BIN"
CLI_MODEL_ENV = "MARIM_CLAUDE_CLI_MODEL"

# Harness tool name → Claude Code tool name. Names with no Claude Code equivalent
# (tree, the LSP navigation tools) are absent on purpose: the CLI has its own
# navigation, so we don't fabricate a mapping. The result feeds --allowedTools.
_CC_TOOL_MAP = {
    "read_file": "Read",
    "glob": "Glob",
    "grep": "Grep",
    "web_search": "WebSearch",
    "fetch_url": "WebFetch",
    "write_file": "Write",
    "edit_file": "Edit",
    "bash": "Bash",
}


class CliUnavailable(Exception):
    """No `claude` binary could be found to back a claude-cli spawn."""


class CliRunError(Exception):
    """The CLI ran but produced no terminal result event (crash / bad output)."""


@dataclass
class CliResult:
    """A finished CLI spawn, shaped like the bits of a Pydantic AI run result the
    spawn lifecycle consumes: the final report text and the run's usage."""

    output: str
    usage: RunUsage


def resolve_cli_binary() -> str | None:
    """The Claude Code executable to spawn: ``$MARIM_CLAUDE_CLI_BIN`` if set, else
    ``claude`` on PATH. Returns an absolute path, or None when nothing is found so
    the caller reports a clean error instead of crashing."""
    name = os.environ.get(CLI_BINARY_ENV) or "claude"
    return shutil.which(name)


def cli_permission_mode(allow_gated: bool) -> str:
    """The ``--permission-mode`` for a spawn: ``acceptEdits`` in auto mode (gated
    tools allowed), else ``plan`` (read-only — the headless CLI can't prompt, so
    anything not pre-authorized is simply unavailable)."""
    return "acceptEdits" if allow_gated else "plan"


def map_tools_to_cc(tool_names) -> list[str]:
    """Translate granted harness tool names to Claude Code ``--allowedTools``
    names, dropping any without a Claude Code equivalent. Sorted for a stable
    argv (and stable tests)."""
    return sorted({_CC_TOOL_MAP[n] for n in tool_names if n in _CC_TOOL_MAP})


def build_cli_argv(
    binary: str,
    prompt: str,
    system_prompt: str,
    permission_mode: str,
    allowed_tools: list[str],
    model: str | None,
) -> list[str]:
    """The argv for one headless spawn. ``stream-json`` requires ``--verbose``.
    The task is a single positional arg (we exec, not shell — no quoting hazard);
    the agent's role prompt is appended to the CLI's own system prompt. ``--model``
    is omitted when None so the CLI uses its configured default; ``--allowedTools``
    is omitted when empty (which, in plan mode, simply leaves the CLI read-only)."""
    argv = [
        binary, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--append-system-prompt", system_prompt,
        "--permission-mode", permission_mode,
    ]
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]
    if model:
        argv += ["--model", model]
    return argv


def synth_usage(cli_usage: dict | None, num_turns: int) -> RunUsage:
    """Build a RunUsage from the CLI ``result`` event's ``usage`` block so the
    turn's token line reflects the spawn. Only tokens are folded — the dollar cost
    is the CLI account's, not the harness provider's. Missing keys default to 0."""
    u = cli_usage or {}
    return RunUsage(
        input_tokens=int(u.get("input_tokens", 0) or 0),
        output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
        requests=int(num_turns or 0),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagents_cli.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/marim_harness/subagents_cli.py tests/test_subagents_cli.py`
Expected: no errors. (`asyncio`/`json`/`datetime`/message-event imports are unused until Task 3/4 — if ruff flags F401, proceed to Task 3 in the same commit window rather than deleting them; they're consumed there. To keep this commit clean, add the translator/runner in Task 3/4 before committing, OR temporarily place only the imports each step needs. Simplest: defer the unused imports — keep only `os`, `shutil`, `dataclass`, `RunUsage`, `logging` here, and add the rest in Task 3.)

Adjust the import block to exactly what Task 2 uses:

```python
import logging
import os
import shutil
from dataclasses import dataclass

from pydantic_ai.result import RunUsage
```

Re-run ruff: `uv run ruff check src/marim_harness/subagents_cli.py` → no errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/subagents_cli.py tests/test_subagents_cli.py
git commit -m "feat(subagents): pure helpers for claude-cli backend"
```

---

## Task 3: `CliStreamTranslator` — stream-json → Pydantic AI events

**Files:**
- Modify: `src/marim_harness/subagents_cli.py` (add imports + `CliStreamTranslator` + `_flatten_tool_result`)
- Test: `tests/test_subagents_cli.py` (append translator tests)

**Interfaces:**
- Consumes: `PartStartEvent`, `PartDeltaEvent`, `TextPart`, `TextPartDelta`, `FunctionToolCallEvent`, `FunctionToolResultEvent`, `ToolCallPart`, `ToolReturnPart` from `pydantic_ai.messages`.
- Produces: `CliStreamTranslator().translate(obj: dict) -> list` of message events. Stateful: numbers parts via `index`, remembers each `tool_use` id→name so the matching `tool_result` is labeled.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_subagents_cli.py
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
)

from marim_harness.subagents_cli import CliStreamTranslator


def test_translate_assistant_text_emits_start_then_full_delta():
    t = CliStreamTranslator()
    events = t.translate({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Hello there"}]},
    })
    assert isinstance(events[0], PartStartEvent)
    assert isinstance(events[1], PartDeltaEvent)
    assert isinstance(events[1].delta, TextPartDelta)
    assert events[1].delta.content_delta == "Hello there"
    # start and its delta share the same part index
    assert events[0].index == events[1].index


def test_translate_tool_use_emits_call_event():
    t = CliStreamTranslator()
    events = t.translate({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "x.py"}},
        ]},
    })
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, FunctionToolCallEvent)
    assert ev.part.tool_name == "Read"
    assert ev.part.tool_call_id == "toolu_1"
    assert ev.part.args_as_dict() == {"path": "x.py"}


def test_translate_tool_result_labels_from_prior_call_and_marks_failure():
    t = CliStreamTranslator()
    t.translate({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": {"command": "ls"}},
        ]},
    })
    events = t.translate({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_9",
             "content": [{"type": "text", "text": "boom"}], "is_error": True},
        ]},
    })
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, FunctionToolResultEvent)
    assert ev.part.tool_name == "Bash"          # carried from the matching call
    assert ev.part.tool_call_id == "toolu_9"
    assert ev.part.content == "boom"            # list-of-blocks flattened to text
    assert ev.part.outcome == "failed"          # is_error → failed


def test_translate_ignores_system_and_result():
    t = CliStreamTranslator()
    assert t.translate({"type": "system", "subtype": "init"}) == []
    assert t.translate({"type": "result", "result": "done"}) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagents_cli.py -k translate -v`
Expected: FAIL — `cannot import name 'CliStreamTranslator'`.

- [ ] **Step 3: Add the translator (and restore the message-event imports)**

Restore the full import block at the top of `subagents_cli.py` (the one from Task 2 Step 3) so the message-event classes are available, then append:

```python
class CliStreamTranslator:
    """Turns parsed Claude Code stream-json objects into the Pydantic AI message
    events the TUI already renders, so a CLI spawn streams nested under its card
    like a native sub-agent. Stateful across a run: numbers parts and remembers
    each tool_use's name so the matching tool_result can be labeled. ``translate``
    returns zero or more events per object; ``system`` and the terminal ``result``
    yield nothing (the runner reads result text/usage separately).

    stream-json without ``--include-partial-messages`` delivers each assistant
    message whole, so a text block becomes an empty part-start plus one full
    delta — the render path's delta branch appends it exactly as for live tokens.
    """

    def __init__(self) -> None:
        self._index = 0
        self._call_names: dict[str, str] = {}

    def translate(self, obj: dict) -> list:
        kind = obj.get("type")
        if kind == "assistant":
            return self._assistant(obj)
        if kind == "user":
            return self._user(obj)
        return []

    def _assistant(self, obj: dict) -> list:
        events: list = []
        for block in obj.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text":
                idx = self._index
                self._index += 1
                events.append(PartStartEvent(index=idx, part=TextPart(content="")))
                events.append(PartDeltaEvent(
                    index=idx,
                    delta=TextPartDelta(content_delta=block.get("text", "")),
                ))
            elif btype == "tool_use":
                call_id = block.get("id", "")
                name = block.get("name", "tool")
                self._call_names[call_id] = name
                events.append(FunctionToolCallEvent(part=ToolCallPart(
                    tool_name=name,
                    args=block.get("input", {}),
                    tool_call_id=call_id,
                )))
        return events

    def _user(self, obj: dict) -> list:
        events: list = []
        for block in obj.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id", "")
            events.append(FunctionToolResultEvent(part=ToolReturnPart(
                tool_name=self._call_names.get(call_id, "tool"),
                content=_flatten_tool_result(block.get("content")),
                tool_call_id=call_id,
                timestamp=datetime.now(tz=timezone.utc),
                outcome="failed" if block.get("is_error") else "success",
            )))
        return events


def _flatten_tool_result(content) -> str:
    """A tool_result's content is either a string or a list of content blocks;
    reduce it to plain text for the card."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return "" if content is None else str(content)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagents_cli.py -v`
Expected: PASS (all helper + translator tests).

- [ ] **Step 5: Lint + type-check**

Run: `uv run ruff check src/marim_harness/subagents_cli.py && uv run pyright src/marim_harness/subagents_cli.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/subagents_cli.py tests/test_subagents_cli.py
git commit -m "feat(subagents): translate claude-cli stream-json into UI events"
```

---

## Task 4: `ClaudeCliRunner` — spawn the process, forward events, capture result

**Files:**
- Modify: `src/marim_harness/subagents_cli.py` (add `ClaudeCliRunner`)
- Test: `tests/test_subagents_cli.py` (append a fake-binary integration test)

**Interfaces:**
- Consumes: `build_cli_argv`, `cli_permission_mode`, `map_tools_to_cc`, `synth_usage`, `CliStreamTranslator`, `CliResult`, `CliRunError`.
- Produces: `ClaudeCliRunner(on_event, on_notice)` with
  `async run(*, binary, prompt, system_prompt, cwd, allow_gated, allowed_tools, model, stream_id) -> CliResult`.
  `on_event` is `Deps.on_subagent_event` (`(stream_id, event, usage)`) or None; `on_notice` is `Deps.on_subagent_notice` or None.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_subagents_cli.py
import os
import stat
import sys

import pytest

from marim_harness.subagents_cli import ClaudeCliRunner

_FAKE_CLI = '''#!{python}
import json, sys
lines = [
    {{"type": "system", "subtype": "init"}},
    {{"type": "assistant", "message": {{"content": [
        {{"type": "text", "text": "Working on it"}},
        {{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {{"path": "x"}}}},
    ]}}}},
    {{"type": "user", "message": {{"content": [
        {{"type": "tool_result", "tool_use_id": "toolu_1", "content": "file body", "is_error": False}},
    ]}}}},
    {{"type": "result", "subtype": "success", "result": "Done: found it",
      "num_turns": 2, "total_cost_usd": 0.001,
      "usage": {{"input_tokens": 10, "output_tokens": 5,
                 "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1}}}},
]
for o in lines:
    sys.stdout.write(json.dumps(o) + "\\n")
'''


def _make_fake_cli(tmp_path) -> str:
    p = tmp_path / "fake_claude.py"
    p.write_text(_FAKE_CLI.format(python=sys.executable), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


@pytest.mark.anyio
async def test_runner_streams_events_and_returns_result(tmp_path):
    binary = _make_fake_cli(tmp_path)
    seen = []

    async def on_event(stream_id, event, usage):
        seen.append((stream_id, type(event).__name__))

    runner = ClaudeCliRunner(on_event, None)
    result = await runner.run(
        binary=binary, prompt="go", system_prompt="be a worker",
        cwd=str(tmp_path), allow_gated=True, allowed_tools=frozenset({"read_file"}),
        model=None, stream_id="s1",
    )
    assert result.output == "Done: found it"
    assert result.usage.input_tokens == 10 and result.usage.output_tokens == 5
    names = [n for _, n in seen]
    assert "FunctionToolCallEvent" in names
    assert "FunctionToolResultEvent" in names
    assert all(sid == "s1" for sid, _ in seen)


@pytest.mark.anyio
async def test_runner_raises_when_no_result(tmp_path):
    p = tmp_path / "silent.py"
    p.write_text(f"#!{sys.executable}\nimport sys; sys.exit(3)\n", encoding="utf-8")
    p.chmod(0o755)
    runner = ClaudeCliRunner(None, None)
    with pytest.raises(Exception) as exc:
        await runner.run(
            binary=str(p), prompt="go", system_prompt="s", cwd=str(tmp_path),
            allow_gated=False, allowed_tools=frozenset(), model=None, stream_id="",
        )
    assert "no result" in str(exc.value).lower()
```

> Note: the repo already configures `anyio`/`pytest` (see existing `@pytest.mark.anyio` tests). If a fixture for the anyio backend is needed, it is already provided in `tests/conftest.py` (the concurrency tests use the same marker).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagents_cli.py -k runner -v`
Expected: FAIL — `cannot import name 'ClaudeCliRunner'`.

- [ ] **Step 3: Add the runner**

Ensure `asyncio` and `json` are imported at the top of `subagents_cli.py` (add them to the import block), then append:

```python
class ClaudeCliRunner:
    """Spawns the Claude Code CLI for one sub-agent task and forwards its activity.

    Reads the process's stream-json stdout line by line, translates each event for
    the UI (when a foreground ``stream_id`` and an ``on_event`` sink are present),
    and captures the terminal ``result`` event's text + usage. Raises CliRunError
    if the process ends without a result. The harness wraps this with hooks,
    output cap, and worktree handling — see SubagentRunner._execute_cli_spawn.
    """

    def __init__(self, on_event, on_notice) -> None:
        self._on_event = on_event      # Deps.on_subagent_event | None
        self._on_notice = on_notice    # Deps.on_subagent_notice | None

    async def run(
        self, *, binary: str, prompt: str, system_prompt: str, cwd: str,
        allow_gated: bool, allowed_tools, model: str | None, stream_id: str,
    ) -> CliResult:
        argv = build_cli_argv(
            binary, prompt, system_prompt,
            cli_permission_mode(allow_gated),
            map_tools_to_cc(allowed_tools), model,
        )
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        translator = CliStreamTranslator()
        output = ""
        usage = RunUsage()
        result_seen = False
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # non-JSON noise on stdout — skip
            if obj.get("type") == "result":
                output = obj.get("result", "") or ""
                usage = synth_usage(obj.get("usage"), obj.get("num_turns", 0) or 0)
                result_seen = True
                continue
            for event in translator.translate(obj):
                if self._on_event is not None and stream_id:
                    await self._on_event(stream_id, event, None)
        stderr_bytes = await proc.stderr.read() if proc.stderr is not None else b""
        code = await proc.wait()
        if not result_seen:
            detail = stderr_bytes.decode("utf-8", "replace").strip() or f"exit code {code}"
            raise CliRunError(f"claude produced no result ({detail})")
        return CliResult(output=output, usage=usage)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagents_cli.py -v`
Expected: PASS (all helper + translator + runner tests).

- [ ] **Step 5: Lint + type-check**

Run: `uv run ruff check src/marim_harness/subagents_cli.py tests/test_subagents_cli.py && uv run pyright src/marim_harness/subagents_cli.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/subagents_cli.py tests/test_subagents_cli.py
git commit -m "feat(subagents): ClaudeCliRunner spawns and streams a claude-cli spawn"
```

---

## Task 5: Wire the CLI backend into `SubagentRunner`

**Files:**
- Modify: `src/marim_harness/subagents.py` (add `import os`; branch in `_execute_spawn`; add `_execute_cli_spawn`, `_cli_mcp_note`, `_run_cli`)
- Test: `tests/test_subagent_cli_spawn.py`

**Interfaces:**
- Consumes: `find_agent`, `effective_tools` (already imported in `subagents.py`); `ClaudeCliRunner`, `CliResult`, `CliUnavailable`, `resolve_cli_binary`, `CLI_MODEL_ENV` from `subagents_cli`; `Mode` (already imported).
- Produces: `SubagentRunner.run(type, task, stream_id)` returns a CLI agent's final report when `defn.backend == "claude-cli"`; native path unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagent_cli_spawn.py
import stat
import sys
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from tests.conftest import _make_harness

_FAKE_CLI = '''#!{python}
import json, sys
for o in [
    {{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "hi"}}]}}}},
    {{"type": "result", "subtype": "success", "result": "Done: report body",
      "num_turns": 1, "usage": {{"input_tokens": 7, "output_tokens": 4}}}},
]:
    sys.stdout.write(json.dumps(o) + "\\n")
'''


def _fake_cli(tmp_path: Path) -> str:
    p = tmp_path / "fake_claude.py"
    p.write_text(_FAKE_CLI.format(python=sys.executable), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


def _write_cli_agent(tmp_path: Path) -> None:
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cli-worker.md").write_text(
        "---\ndescription: CLI worker\nbackend: claude-cli\ntools: read_file\n---\n"
        "You are a CLI worker.\n",
        encoding="utf-8",
    )


def _dummy_model() -> FunctionModel:
    async def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="unused")])
    return FunctionModel(fn)


@pytest.mark.anyio
async def test_cli_backend_spawn_returns_report(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _write_cli_agent(tmp_path)
    runner = _make_harness(
        _dummy_model(), Deps(workspace_root=tmp_path, mode=Mode.auto)
    ).subagents
    out = await runner.run("cli-worker", "do the thing", stream_id="s1")
    assert "Done: report body" in out
    assert runner.session.usage.output_tokens == 4


@pytest.mark.anyio
async def test_cli_backend_missing_binary_is_contained(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", "no-such-claude-binary")
    _write_cli_agent(tmp_path)
    runner = _make_harness(
        _dummy_model(), Deps(workspace_root=tmp_path, mode=Mode.auto)
    ).subagents
    out = await runner.run("cli-worker", "do the thing", stream_id="s1")
    assert "failed" in out.lower()  # contained, not raised


@pytest.mark.anyio
async def test_cli_backend_notes_unforwarded_mcp(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _write_cli_agent(tmp_path)
    runner = _make_harness(
        _dummy_model(), Deps(workspace_root=tmp_path, mode=Mode.auto)
    ).subagents
    out = await runner.run("cli-worker", "t", stream_id="s1", mcp_names=["mddocs"])
    assert "mddocs" in out and "not forwarded" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagent_cli_spawn.py -v`
Expected: FAIL — the CLI agent is parsed but `_execute_spawn` runs the native `build()` path, which builds a Pydantic AI agent on the dummy model and returns `"unused"` (or errors), not `"Done: report body"`.

- [ ] **Step 3: Add `import os` and the branch in `_execute_spawn`**

At the top of `subagents.py`, add `import os` to the stdlib import group.

In `_execute_spawn`, immediately after `work_root = iso["path"] if iso else None` (currently line ~397) and before `sub, err = self.build(...)`, insert:

```python
        # CLI-backed agents run an external `claude` process instead of the
        # in-process Pydantic AI loop. Branch here so everything below stays the
        # native path, byte-for-byte. The CLI path mirrors the same wrapper
        # (hooks bracketing, output cap, worktree, background persist) in
        # _execute_cli_spawn — duplicated deliberately to keep the native flow
        # untouched; both halves are small and evolve independently.
        defn = find_agent(self.deps.workspace_root, type)
        if defn is not None and defn.backend == "claude-cli":
            return await self._execute_cli_spawn(
                defn, task, work_root, iso, mcp_names, max_output_chars,
                model, stream_id, background=background,
            )
```

- [ ] **Step 4: Add the three methods**

Add these methods to `SubagentRunner` (place them just after `_execute_spawn`):

```python
    async def _execute_cli_spawn(
        self, defn, task: str, work_root, iso,
        mcp_names: list[str] | None, max_output_chars: int | None,
        model: str | None, stream_id: str, *, background: bool,
    ) -> str:
        """Run a ``backend: claude-cli`` agent inside the same lifecycle the native
        path uses: hooks bracketing, output cap/spill, worktree close, background
        persist. Harness MCP grants are NOT forwarded to the CLI (it uses its own
        MCP config); a non-empty ``mcp_names`` is noted, not honored.

        Mirrors _execute_spawn's foreground/background contract: foreground
        contains a failure as an error string (so a sibling fan-out spawn isn't
        taken down); background re-raises to the job registry. Usage is folded into
        the session, and a background spawn persists immediately since no run_turn
        will fold its spend."""
        await self.hooks.subagent_start(defn.name, task)
        try:
            async with self._slot():
                result = await self._run_cli(defn, task, work_root, model, stream_id)
        except Exception as exc:  # noqa: BLE001
            if iso:
                self._discard_worktree(iso)
            if background:
                raise
            await self.hooks.subagent_stop(defn.name, task, f"error: {exc}")
            return f"Sub-agent {defn.name!r} failed: {exc.__class__.__name__}: {exc}"
        await self.hooks.subagent_stop(defn.name, task, result.output)
        self.session.usage += result.usage
        if background:
            self.session.persist()
            self._bg_seq += 1
            spill_ref = f"bg-{self._bg_seq}"
        else:
            spill_ref = stream_id
        capped = self._cap_output(result.output, max_output_chars, spill_ref)
        iso_note = self._close_worktree(iso) if iso else ""
        return self._cli_mcp_note(mcp_names) + capped + iso_note

    @staticmethod
    def _cli_mcp_note(mcp_names: list[str] | None) -> str:
        """A one-line note when the orchestrator named MCP servers for a CLI spawn:
        they aren't forwarded (the CLI uses its own MCP config), so say so rather
        than silently dropping them."""
        if not mcp_names:
            return ""
        names = ", ".join(mcp_names)
        return (
            f"[note: MCP servers ({names}) are not forwarded to claude-cli "
            "sub-agents; configure them in the CLI's own settings]\n\n"
        )

    async def _run_cli(self, defn, task: str, work_root, model: str | None,
                       stream_id: str) -> "CliResult":
        """Resolve binary, tool reach, model, and cwd for a CLI spawn, then run it.
        Raises CliUnavailable when no `claude` binary is found so the caller's
        contained-error path reports it. Reach mirrors the native gate — gated
        tools only in auto mode. Model precedence: per-spawn override, then the
        agent's frontmatter model, then $MARIM_CLAUDE_CLI_MODEL, then the CLI's
        own default."""
        from .subagents_cli import (
            CLI_MODEL_ENV,
            ClaudeCliRunner,
            CliUnavailable,
            resolve_cli_binary,
        )

        binary = resolve_cli_binary()
        if binary is None:
            raise CliUnavailable(
                "no `claude` binary found (set MARIM_CLAUDE_CLI_BIN or install "
                "Claude Code)"
            )
        allow_gated = self.deps.mode is Mode.auto
        tools = effective_tools(defn, allow_gated=allow_gated)
        cwd = str(work_root or self.deps.workspace_root)
        model_name = model or defn.model or os.environ.get(CLI_MODEL_ENV)
        runner = ClaudeCliRunner(
            self.deps.on_subagent_event, self.deps.on_subagent_notice,
        )
        return await runner.run(
            binary=binary, prompt=task, system_prompt=defn.prompt, cwd=cwd,
            allow_gated=allow_gated, allowed_tools=tools, model=model_name,
            stream_id=stream_id,
        )
```

Add the `CliResult` import for the return annotation. Since it's only used in a string annotation, no runtime import is needed; add it under `TYPE_CHECKING` at the top of `subagents.py`:

```python
if TYPE_CHECKING:
    from .mcp.manager import McpManager
    from .session.ctrl import SessionController
    from .subagents_cli import CliResult
    from .tools.provider import ToolProvider
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagent_cli_spawn.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the existing sub-agent tests to confirm the native path is untouched**

Run: `uv run pytest --no-cov tests/test_subagent_tool.py tests/test_subagent_concurrency.py -v`
Expected: PASS (all pre-existing tests).

- [ ] **Step 7: Lint + type-check**

Run: `uv run ruff check src/marim_harness/subagents.py tests/test_subagent_cli_spawn.py && uv run pyright src/marim_harness/subagents.py`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/subagents.py tests/test_subagent_cli_spawn.py
git commit -m "feat(subagents): route backend:claude-cli agents to the CLI runner"
```

---

## Task 6: Document the model-alias hint and ship an example agent

**Files:**
- Modify: `src/marim_harness/tools/provider.py` (one paragraph in the `spawn_agent` docstring, near the existing `model` description)
- Create: `docs/examples/agents/cli-worker.md` (a ready-to-copy example CLI agent)
- Test: `tests/test_agent_backend_field.py` (assert the example file parses as a claude-cli agent)

**Interfaces:**
- Consumes: `AgentDef.backend`/`AgentDef.model` (Task 1), `_parse_agent` discovery.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_agent_backend_field.py
import shutil
from pathlib import Path

from marim_harness.workspace.agents import find_agent as _find_agent


def test_example_cli_agent_parses_as_claude_cli(tmp_path: Path):
    src = Path("docs/examples/agents/cli-worker.md")
    dst = tmp_path / ".marim" / "agents" / "cli-worker.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    defn = _find_agent(tmp_path, "cli-worker")
    assert defn is not None
    assert defn.backend == "claude-cli"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_agent_backend_field.py -k example -v`
Expected: FAIL — `FileNotFoundError` (the example file doesn't exist yet).

- [ ] **Step 3: Create the example agent**

```markdown
<!-- docs/examples/agents/cli-worker.md -->
---
description: Autonomous worker backed by the Claude Code CLI (claude -p).
backend: claude-cli
model: sonnet
tools: read_file, glob, grep, edit_file, write_file, bash
---
You are an autonomous worker. Carry out the task you are given end-to-end using
your own tools, keep changes minimal and focused, then report what you did and any
results as your final message.
```

- [ ] **Step 4: Add the model-alias note to `spawn_agent`'s docstring**

In `src/marim_harness/tools/provider.py`, in the `spawn_agent` docstring near where the `model` parameter is described, add:

```
For a sub-agent whose definition sets `backend: claude-cli`, `model` is a Claude
Code model name (an alias like `opus`, `sonnet`, `haiku`, or `fable`, or a full id
like `claude-sonnet-4-6`) passed straight to the CLI — not a harness/OpenRouter
model id. Omit it to use the agent's own `model:` default or the CLI's configured
default.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_agent_backend_field.py -v`
Expected: PASS.

- [ ] **Step 6: Lint**

Run: `uv run ruff check tests/test_agent_backend_field.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add docs/examples/agents/cli-worker.md src/marim_harness/tools/provider.py tests/test_agent_backend_field.py
git commit -m "docs(subagents): example claude-cli agent and model-alias hint"
```

---

## Task 7: Full-suite green + final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full CI sequence locally**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: ruff clean, pyright clean, all tests pass (coverage on by default).

- [ ] **Step 2: Manual smoke test (optional, needs a real `claude` on PATH)**

With Claude Code installed and authenticated, copy `docs/examples/agents/cli-worker.md` to a trusted project's `.marim/agents/`, launch the harness in `auto` mode, and ask the main agent to `spawn_agent(type="cli-worker", task="list the python files and summarize the package layout")`. Confirm the spawn card streams tool calls and returns a report.

- [ ] **Step 3: Commit any final cleanup** (only if Step 1 surfaced fixes)

```bash
git add -A && git commit -m "chore(subagents): tidy after full-suite verification"
```

---

## Self-Review

**Spec coverage** (each spec section → task):
- §"The seam" / branch in `_execute_spawn` → Task 5.
- §Decisions "Declaration via frontmatter" (`backend`/`model`) → Task 1.
- §Decisions "Permissions mirror native gating" + §"Permission mapping" → Task 2 (`cli_permission_mode`, `map_tools_to_cc`), applied in Task 5 (`_run_cli` uses `effective_tools` + `Mode.auto`).
- §Decisions "Full event streaming, notice fallback" + §"Streaming → UI" → Task 3 (translator) + Task 4 (forwarding). The `on_subagent_notice` fallback path is available (runner holds `on_notice`) but not required for v1; full mapping is implemented.
- §Decisions "Model is backend-dependent, passed through unvalidated" + §"Model resolution" → Task 5 (`_run_cli` precedence), Task 6 (docstring hint). No validation anywhere — confirmed.
- §Decisions "Opt-in worktree" → Task 5 reuses the existing `isolation` arg and `iso` handling unchanged; no auto-isolation added.
- §Decisions "No transient-retry/resume" → Task 5 calls `_run_cli` directly, not `_run_to_completion`; no retry loop.
- §"The CLI runner" (binary resolve, argv, spawn, translate, return) → Tasks 2 + 4.
- §"Output, billing & usage" → Task 2 (`synth_usage`, tokens only) + Task 5 (`session.usage += result.usage`, `_cap_output`).
- §"Trust" (hooks don't reach inside CLI) → Task 5: `_execute_cli_spawn` brackets with `subagent_start/stop` only and never installs the native `handler` (which fires PreToolUse/PostToolUse). MCP-not-forwarded note → `_cli_mcp_note`.
- §"Error handling" (contained vs propagate) → Task 5 mirrors native fg/bg semantics; missing binary → `CliUnavailable` contained.
- §"Testing" (pure helpers direct; one fake-binary integration) → Tasks 2/3 (pure), Task 4 (runner integration), Task 5 (end-to-end).
- §"Out of scope" items → none implemented (no auto-isolation, no MCP-into-CLI, no model enumeration, no CLI retry). Confirmed.

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". Every code step shows full code. ✓

**Type consistency:**
- `CliResult(output: str, usage: RunUsage)` defined Task 2, returned by `ClaudeCliRunner.run` (Task 4) and `_run_cli` (Task 5), consumed as `result.output`/`result.usage` (Task 5) — matches the native `result.output`/`result.usage` shape used at `subagents.py:445-446`. ✓
- `ClaudeCliRunner.__init__(on_event, on_notice)` (Task 4) ← called with `self.deps.on_subagent_event, self.deps.on_subagent_notice` (Task 5), which are `SubAgentEventCb | None` / `SubAgentNoticeCb | None`. ✓
- `ClaudeCliRunner.run(*, binary, prompt, system_prompt, cwd, allow_gated, allowed_tools, model, stream_id)` — same keyword names used in Task 4 test and Task 5 `_run_cli`. ✓
- Event constructors match the introspected 1.107.0 signatures: `PartStartEvent(index=, part=)`, `PartDeltaEvent(index=, delta=)`, `TextPart(content=)`, `TextPartDelta(content_delta=)`, `ToolCallPart(tool_name=, args=, tool_call_id=)`, `ToolReturnPart(tool_name=, content=, tool_call_id=, timestamp=, outcome=)`, `FunctionToolCallEvent(part=)`, `FunctionToolResultEvent(part=)`. `outcome` ∈ `{"success","failed","denied"}`. ✓
- `RunUsage(input_tokens=, output_tokens=, cache_read_tokens=, cache_write_tokens=, requests=)` — matches introspected constructor. ✓
- `effective_tools(defn, allow_gated=...)`, `find_agent(workspace_root, name)`, `Mode.auto` — existing signatures, already imported in `subagents.py`. ✓
```
