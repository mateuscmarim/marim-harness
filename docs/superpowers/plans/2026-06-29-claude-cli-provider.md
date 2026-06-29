# Claude CLI Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `claude-cli` model provider so marim can run turns on a Claude Pro/Max subscription by shelling out to `claude -p`, selectable alongside `openrouter`/`local`/`google`.

**Architecture:** A custom Pydantic AI `Model` (`ClaudeCliModel`) plugs into the existing `build_model()` seam and spawns `claude -p` in `stream-json` mode. Claude runs its own tools/loop internally; the model returns a single **text-only** `ModelResponse` (never `ToolCallPart`s — those would make pydantic_ai re-execute), so marim's turn loop, approval rounds, and resumability are untouched. Conversation continuity uses Claude's `--resume <session_id>` (held in-memory on the model), flattening full history on a cold first turn.

**Tech Stack:** Python 3.10+, Pydantic AI 1.107, asyncio subprocess, the existing pure helpers in `subagents/cli_backend.py`.

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax (no `X | Y` in runtime `isinstance`, no `tomllib`, etc.). Type-hint unions in annotations are fine (`from __future__ import annotations` is used).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced). Run `uv run ruff check --fix src tests`.
- Type-check clean under `uv run pyright` (basic mode, src only).
- Use `uv` for everything (`uv run pytest`, `uv run ruff`, `uv run pyright`). Never bare `python`/`pip`/`pytest`.
- The model's `ModelResponse.parts` MUST contain only text (a single `TextPart`). Never emit `ToolCallPart`s from this model.
- CI order is ruff → pyright → pytest; match it locally before claiming done.
- New module is imported **lazily** inside `build_model` so config-only code paths stay dependency-free (mirrors `openrouter_cost` / `google`).

---

### Task 1: Pure helpers — permission mapping, message extraction, usage

**Files:**
- Create: `src/marim_harness/config/claude_cli_model.py`
- Test: `tests/test_claude_cli_model.py`

**Interfaces:**
- Consumes: nothing from earlier tasks. From pydantic_ai: `ModelMessage`, `ModelRequest`, `ModelResponse`, `UserPromptPart`, `SystemPromptPart`, `TextPart`, `RequestUsage`.
- Produces:
  - `permission_mode_for(mode: str) -> str` — maps marim mode (`"auto"`/`"ask"`/`"plan"`) to a Claude `--permission-mode` (`"acceptEdits"`/`"plan"`).
  - `extract_system(messages: list) -> str` — the system/instructions text to pass via `--append-system-prompt`.
  - `latest_user_text(messages: list) -> str` — text of the newest user prompt.
  - `flatten_history(messages: list) -> str` — the whole conversation rendered to one prompt (cold-start re-seed).
  - `request_usage_from_cli(cli_usage: dict | None, total_cost_usd: float | None) -> RequestUsage`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claude_cli_model.py
from datetime import datetime, timezone

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)

from marim_harness.config.claude_cli_model import (
    extract_system,
    flatten_history,
    latest_user_text,
    permission_mode_for,
    request_usage_from_cli,
)


def test_permission_mode_mapping():
    assert permission_mode_for("auto") == "acceptEdits"
    assert permission_mode_for("ask") == "acceptEdits"
    assert permission_mode_for("plan") == "plan"
    # Unknown falls back to the safe read-only plan mode.
    assert permission_mode_for("weird") == "plan"


def test_latest_user_text_takes_newest_request():
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="first")]),
        ModelResponse(parts=[TextPart(content="answer one")]),
        ModelRequest(parts=[UserPromptPart(content="second")]),
    ]
    assert latest_user_text(msgs) == "second"


def test_latest_user_text_joins_list_content():
    msgs = [ModelRequest(parts=[UserPromptPart(content=["a", "b"])])]
    assert latest_user_text(msgs) == "a\nb"


def test_extract_system_prefers_instructions_then_system_parts():
    msgs = [
        ModelRequest(
            parts=[SystemPromptPart(content="sys-part")],
            instructions="the-instructions",
        ),
    ]
    assert extract_system(msgs) == "the-instructions"
    msgs2 = [ModelRequest(parts=[SystemPromptPart(content="sys-only")])]
    assert extract_system(msgs2) == "sys-only"


def test_flatten_history_labels_roles():
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="hello")]),
        ModelResponse(parts=[TextPart(content="hi there")]),
        ModelRequest(parts=[UserPromptPart(content="more")]),
    ]
    out = flatten_history(msgs)
    assert "User: hello" in out
    assert "Assistant: hi there" in out
    assert out.rstrip().endswith("User: more")


def test_request_usage_folds_cache_and_cost():
    u = request_usage_from_cli(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 7,
        },
        total_cost_usd=0.25,
    )
    assert u.input_tokens == 117  # 10 + 100 + 7, inclusive of cache
    assert u.output_tokens == 5
    assert u.cache_read_tokens == 100
    assert u.cache_write_tokens == 7
    from marim_harness.usage import COST_DETAIL_KEY

    assert u.details[COST_DETAIL_KEY] == 250_000
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_claude_cli_model.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.config.claude_cli_model'`

- [ ] **Step 3: Write the implementation**

```python
# src/marim_harness/config/claude_cli_model.py
"""Run the Claude Code CLI (`claude -p`) as a main-loop model provider.

A Claude subscription is reachable only through the `claude` CLI, which runs its
own agentic loop — there is no raw per-step model endpoint behind it. So this
provider makes marim a *launcher*: ``ClaudeCliModel`` spawns ``claude -p`` in
stream-json mode, lets Claude run its own tools internally, and returns a single
**text-only** ``ModelResponse``. Emitting ``ToolCallPart``s here would make
pydantic_ai's agent graph try to execute Claude's tool calls a second time, so
Claude's internal tool activity is folded into the streamed text instead (see
``format_activity_line`` / ``consume_cli_stream``).

This module reuses the pure helpers in ``subagents.cli_backend`` (binary resolve,
argv build, ndjson reader) and only depends on ``pydantic_ai`` + ``..usage``, so
``config.build_model`` can import it lazily without a cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.usage import RequestUsage

from ..usage import COST_DETAIL_KEY

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

# marim approval mode -> Claude Code --permission-mode. Headless `claude -p`
# cannot pop a per-tool prompt, so marim's "ask" has no faithful equivalent; we
# treat it like "auto" (acceptEdits) and warn once (see note_ask_limitation_once).
# Anything unrecognized degrades to the safe read-only "plan".
_MODE_MAP = {"auto": "acceptEdits", "ask": "acceptEdits", "plan": "plan"}


def permission_mode_for(mode: str) -> str:
    """The Claude ``--permission-mode`` for a marim approval mode."""
    return _MODE_MAP.get(mode, "plan")


def _part_text(content) -> str:
    """A UserPromptPart/TextPart content reduced to plain text. Content is a str
    or a list whose str items are joined (non-str multimodal items are skipped)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c for c in content if isinstance(c, str))
    return "" if content is None else str(content)


def latest_user_text(messages: list[ModelMessage]) -> str:
    """Text of the newest user prompt (what we send to ``claude -p`` each turn)."""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    for msg in reversed(messages):
        if isinstance(msg, ModelRequest):
            texts = [
                _part_text(p.content) for p in msg.parts if isinstance(p, UserPromptPart)
            ]
            if texts:
                return "\n".join(t for t in texts if t)
    return ""


def extract_system(messages: list[ModelMessage]) -> str:
    """The system text for ``--append-system-prompt``: the most recent request's
    ``instructions`` if present, else the concatenated SystemPromptPart content."""
    from pydantic_ai.messages import ModelRequest, SystemPromptPart

    for msg in reversed(messages):
        if isinstance(msg, ModelRequest) and getattr(msg, "instructions", None):
            return str(msg.instructions)
    sys_parts: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            sys_parts += [
                _part_text(p.content) for p in msg.parts if isinstance(p, SystemPromptPart)
            ]
    return "\n".join(s for s in sys_parts if s)


def flatten_history(messages: list[ModelMessage]) -> str:
    """The whole conversation rendered to one prompt, for a cold first turn (no
    Claude session to resume). User turns and our prior text answers only —
    this model never produces tool-call parts, so there are none to render."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )

    lines: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for p in msg.parts:
                if isinstance(p, UserPromptPart):
                    text = _part_text(p.content)
                    if text:
                        lines.append(f"User: {text}")
        elif isinstance(msg, ModelResponse):
            for p in msg.parts:
                if isinstance(p, TextPart) and p.content:
                    lines.append(f"Assistant: {p.content}")
    return "\n\n".join(lines)


def request_usage_from_cli(
    cli_usage: dict | None, total_cost_usd: float | None
) -> RequestUsage:
    """Build a ``RequestUsage`` from the CLI ``result`` event's usage block.

    Mirrors ``cli_backend.synth_usage`` (which returns a RunUsage for sub-agents):
    Anthropic reports ``input_tokens`` as the uncached bucket only, so we fold the
    cache read/write buckets back in to match the harness's cache-inclusive
    convention, and store the billed cost as integer micro-USD under
    ``details[COST_DETAIL_KEY]`` so the cost display needs no model-id lookup."""
    u = cli_usage or {}
    details: dict = {}
    if total_cost_usd is not None:
        details[COST_DETAIL_KEY] = int(total_cost_usd * 1_000_000)
    cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(u.get("cache_creation_input_tokens", 0) or 0)
    uncached_in = int(u.get("input_tokens", 0) or 0)
    return RequestUsage(
        input_tokens=uncached_in + cache_read + cache_write,
        output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        details=details,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_claude_cli_model.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check --fix src/marim_harness/config/claude_cli_model.py tests/test_claude_cli_model.py && uv run pyright src/marim_harness/config/claude_cli_model.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/config/claude_cli_model.py tests/test_claude_cli_model.py
git commit -m "feat: claude-cli model helpers (mode map, message extraction, usage)"
```

---

### Task 2: Extend `build_cli_argv` for `--resume` and an optional system prompt

**Files:**
- Modify: `src/marim_harness/subagents/cli_backend.py:135-158` (`build_cli_argv`)
- Test: `tests/test_cli_backend.py` (add cases; create the file if absent)

**Interfaces:**
- Consumes: existing `build_cli_argv(binary, prompt, system_prompt, permission_mode, allowed_tools, model)`.
- Produces: same function with two new **keyword-only, defaulted** params so existing sub-agent callers are unaffected:
  `build_cli_argv(..., *, resume_session_id: str | None = None, append_system: bool = True)`.
  When `resume_session_id` is set, argv includes `--resume <id>`. When `append_system` is False, `--append-system-prompt` is omitted.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_backend.py  (add these; keep any existing tests)
from marim_harness.subagents.cli_backend import build_cli_argv


def test_build_cli_argv_resume_and_no_system():
    argv = build_cli_argv(
        "claude",
        "do the thing",
        "SYSTEM",
        "acceptEdits",
        [],
        None,
        resume_session_id="sess-123",
        append_system=False,
    )
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "sess-123"
    assert "--append-system-prompt" not in argv


def test_build_cli_argv_defaults_unchanged():
    # Existing sub-agent call shape must still include the system prompt and no resume.
    argv = build_cli_argv("claude", "task", "SYSTEM", "plan", ["Read"], "sonnet")
    assert "--append-system-prompt" in argv
    assert "--resume" not in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cli_backend.py -v`
Expected: FAIL — `build_cli_argv() got an unexpected keyword argument 'resume_session_id'`

- [ ] **Step 3: Edit `build_cli_argv`**

Replace the function body at `src/marim_harness/subagents/cli_backend.py:135-158` with:

```python
def build_cli_argv(
    binary: str,
    prompt: str,
    system_prompt: str,
    permission_mode: str,
    allowed_tools: list[str],
    model: str | None,
    *,
    resume_session_id: str | None = None,
    append_system: bool = True,
) -> list[str]:
    """The argv for one headless spawn. ``stream-json`` requires ``--verbose``.
    The task is a single positional arg (we exec, not shell — no quoting hazard);
    the agent's role prompt is appended to the CLI's own system prompt. ``--model``
    is omitted when None so the CLI uses its configured default; ``--allowedTools``
    is omitted when empty (which, in plan mode, simply leaves the CLI read-only).

    The main-loop ``ClaudeCliModel`` uses ``resume_session_id`` to continue an
    existing Claude session (sending only the new user message), and sets
    ``append_system=False`` on those resumed turns so the system prompt — already
    set when the session was created — is not appended again."""
    argv = [
        binary, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", permission_mode,
    ]
    if append_system:
        argv += ["--append-system-prompt", system_prompt]
    if resume_session_id:
        argv += ["--resume", resume_session_id]
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]
    if model:
        argv += ["--model", model]
    return argv
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli_backend.py -v`
Expected: PASS (both new tests; any pre-existing tests still green)

- [ ] **Step 5: Run the existing sub-agent CLI tests to confirm no regression**

Run: `uv run pytest -k "cli" -v`
Expected: PASS (the sub-agent backend still builds argv as before)

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/subagents/cli_backend.py tests/test_cli_backend.py
git commit -m "feat: build_cli_argv supports --resume and optional --append-system-prompt"
```

---

### Task 3: Activity line + the stream-consumer core (`consume_cli_stream`)

**Files:**
- Modify: `src/marim_harness/config/claude_cli_model.py`
- Test: `tests/test_claude_cli_model.py` (add cases)

**Interfaces:**
- Consumes: `request_usage_from_cli` (Task 1).
- Produces:
  - `format_activity_line(name: str, tool_input: dict) -> str` — a one-line `⏺ <Tool> <summary>` for a Claude tool_use block.
  - `TextChunk(delta: str)` and `DoneChunk(text: str, session_id: str | None, usage: RequestUsage, complete: bool)` dataclasses.
  - `async def consume_cli_stream(objs) -> AsyncIterator[TextChunk | DoneChunk]` — turns parsed stream-json dicts into text chunks (assistant text + folded activity lines) and a terminal `DoneChunk`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claude_cli_model.py  (add)
import pytest

from marim_harness.config.claude_cli_model import (
    DoneChunk,
    TextChunk,
    consume_cli_stream,
    format_activity_line,
)


def test_format_activity_line_summarizes_common_tools():
    assert format_activity_line("Read", {"file_path": "a/b.py"}) == "⏺ Read a/b.py"
    assert format_activity_line("Bash", {"command": "ls -la"}) == "⏺ Bash ls -la"
    assert format_activity_line("Grep", {"pattern": "foo"}) == "⏺ Grep foo"
    # Unknown tool: name only, no crash.
    assert format_activity_line("TodoWrite", {"todos": []}) == "⏺ TodoWrite"


async def _collect(objs):
    async def gen():
        for o in objs:
            yield o

    out = []
    async for chunk in consume_cli_stream(gen()):
        out.append(chunk)
    return out


@pytest.mark.anyio
async def test_consume_streams_text_activity_and_done():
    objs = [
        {"type": "system", "subtype": "init", "session_id": "sess-9", "model": "claude-x"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Looking…"}]},
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "x.py"}}
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Done."}]},
        },
        {
            "type": "result",
            "result": "Done.",
            "session_id": "sess-9",
            "num_turns": 2,
            "usage": {"input_tokens": 3, "output_tokens": 4},
            "total_cost_usd": 0.01,
        },
    ]
    chunks = await _collect(objs)
    texts = [c.delta for c in chunks if isinstance(c, TextChunk)]
    assert texts[0] == "Looking…"
    assert "⏺ Read x.py" in "".join(texts)
    assert texts[-1] == "Done."
    done = chunks[-1]
    assert isinstance(done, DoneChunk)
    assert done.session_id == "sess-9"
    assert done.complete is True
    assert done.usage.output_tokens == 4
    # Final text is the concatenation of everything streamed.
    assert done.text == "".join(texts)


@pytest.mark.anyio
async def test_consume_marks_incomplete_when_no_result():
    objs = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    chunks = await _collect(objs)
    done = chunks[-1]
    assert isinstance(done, DoneChunk)
    assert done.complete is False
    assert done.text == "hi"
```

If the repo has no `anyio`/`asyncio` pytest plugin configured, add a fixture instead. Check first:

Run: `uv run python -c "import anyio; print('anyio ok')"` and `grep -n "asyncio_mode\|anyio" pyproject.toml`.
- If `anyio` is present and `tests/conftest.py` defines an `anyio_backend` fixture, keep `@pytest.mark.anyio`.
- Otherwise replace each `@pytest.mark.anyio` with `@pytest.mark.asyncio` if `pytest-asyncio` is configured, or drive the coroutine with `asyncio.run(_collect(objs))` inside a plain `def test_...`. Pick whichever matches the existing async tests in `tests/` (grep: `grep -rn "pytest.mark.anyio\|pytest.mark.asyncio\|asyncio.run" tests | head`).

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_claude_cli_model.py -k "activity or consume" -v`
Expected: FAIL with `ImportError: cannot import name 'consume_cli_stream'`

- [ ] **Step 3: Add the implementation**

Append to `src/marim_harness/config/claude_cli_model.py`:

```python
from collections.abc import AsyncIterator  # add to the import block at top
from dataclasses import dataclass, field  # add to the import block at top


# Claude tool_use -> the single arg worth showing on the activity line. Tools not
# listed render as the bare name. Mirrors the TUI's native label keys.
_ACTIVITY_ARG = {
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Bash": "command",
    "Grep": "pattern",
    "Glob": "pattern",
    "WebSearch": "query",
    "WebFetch": "url",
}


def format_activity_line(name: str, tool_input: dict) -> str:
    """A compact ``⏺ <Tool> <summary>`` line for one Claude tool_use, folded into
    the streamed text so the user sees progress (we cannot surface real tool-call
    parts — pydantic_ai would try to execute them)."""
    key = _ACTIVITY_ARG.get(name)
    summary = ""
    if key:
        raw = tool_input.get(key, "")
        summary = " " + str(raw).strip().splitlines()[0] if str(raw).strip() else ""
    return f"⏺ {name}{summary}"


@dataclass
class TextChunk:
    """A piece of visible text (assistant prose or a folded activity line)."""

    delta: str


@dataclass
class DoneChunk:
    """Terminal chunk: the full text, Claude's session id, usage, and whether a
    proper ``result`` event was seen (``complete=False`` ⇒ crash/bad output)."""

    text: str
    session_id: str | None
    usage: RequestUsage
    complete: bool


async def consume_cli_stream(objs: AsyncIterator[dict]) -> AsyncIterator:
    """Turn parsed stream-json objects into ``TextChunk``s then one ``DoneChunk``.

    Assistant ``text`` blocks stream as-is; ``tool_use`` blocks are folded into the
    text as ``format_activity_line`` output. The terminal ``result`` event yields a
    ``DoneChunk`` carrying usage + session id. If the stream ends without a
    ``result``, the final ``DoneChunk`` has ``complete=False``."""
    text_parts: list[str] = []
    session_id: str | None = None
    async for obj in objs:
        kind = obj.get("type")
        if kind == "system":
            session_id = session_id or obj.get("session_id")
        elif kind == "assistant":
            message = obj.get("message") or {}
            for block in message.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    delta = block.get("text", "") or ""
                    if delta:
                        text_parts.append(delta)
                        yield TextChunk(delta)
                elif btype == "tool_use":
                    line = "\n" + format_activity_line(
                        block.get("name", "tool"), block.get("input") or {}
                    )
                    text_parts.append(line)
                    yield TextChunk(line)
        elif kind == "result":
            session_id = session_id or obj.get("session_id")
            yield DoneChunk(
                text="".join(text_parts),
                session_id=session_id,
                usage=request_usage_from_cli(obj.get("usage"), obj.get("total_cost_usd")),
                complete=True,
            )
            return
    yield DoneChunk(
        text="".join(text_parts), session_id=session_id, usage=RequestUsage(), complete=False
    )
```

Move the two new `import` lines (`AsyncIterator`, `dataclass`/`field`) up into the existing top-of-file import block rather than mid-file (ruff `E402`). `field` may be unused — drop it if so to satisfy ruff `F401`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_claude_cli_model.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 5: Lint**

Run: `uv run ruff check --fix src/marim_harness/config/claude_cli_model.py tests/test_claude_cli_model.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/config/claude_cli_model.py tests/test_claude_cli_model.py
git commit -m "feat: claude-cli stream consumer with folded tool-activity lines"
```

---

### Task 4: `ClaudeCliModel` + streamed response (`request` / `request_stream`)

**Files:**
- Modify: `src/marim_harness/config/claude_cli_model.py`
- Test: `tests/test_claude_cli_model.py` (add cases)

**Interfaces:**
- Consumes: `consume_cli_stream`, `TextChunk`, `DoneChunk` (Task 3); `permission_mode_for`, `extract_system`, `latest_user_text`, `flatten_history` (Task 1); from `..subagents.cli_backend`: `resolve_cli_binary`, `build_cli_argv`, `_iter_ndjson_lines`.
- Produces:
  - `class ClaudeCliModel(Model)` with `__init__(self, model_id: str | None)`, public attrs `mode_getter: Callable[[], str] | None` and `session_id: str | None`, and `spawn = spawn_cli_objects` (instance hook so tests can monkeypatch).
  - `async def spawn_cli_objects(argv: list[str], cwd: str) -> AsyncIterator[dict]`.
  - `class CliModelError(Exception)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claude_cli_model.py  (add)
import pytest

from marim_harness.config.claude_cli_model import ClaudeCliModel, CliModelError


def _fake_objs(objs):
    async def _spawn(argv, cwd):
        for o in objs:
            yield o

    return _spawn


_INIT = {"type": "system", "subtype": "init", "session_id": "S1", "model": "claude-x"}


def _result(text, sid="S1"):
    return {
        "type": "result",
        "result": text,
        "session_id": sid,
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "total_cost_usd": 0.0,
    }


def _user(text):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    return [ModelRequest(parts=[UserPromptPart(content=text)], instructions="SYS")]


@pytest.mark.anyio
async def test_request_returns_text_only_response_and_captures_session():
    model = ClaudeCliModel("sonnet")
    model.spawn = _fake_objs(
        [
            _INIT,
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
            _result("hello"),
        ]
    )
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.messages import TextPart, ToolCallPart

    resp = await model.request(_user("hi"), None, ModelRequestParameters())
    assert [type(p) for p in resp.parts] == [TextPart]
    assert resp.parts[0].content == "hello"
    assert not any(isinstance(p, ToolCallPart) for p in resp.parts)
    assert model.session_id == "S1"  # captured for the next turn


@pytest.mark.anyio
async def test_second_turn_uses_resume(monkeypatch):
    model = ClaudeCliModel("sonnet")
    model.session_id = "S1"
    captured = {}

    def _spawn(argv, cwd):
        captured["argv"] = argv

        async def gen():
            yield _INIT
            yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
            yield _result("ok", sid="S1")

        return gen()

    model.spawn = _spawn
    from pydantic_ai.models import ModelRequestParameters

    await model.request(_user("again"), None, ModelRequestParameters())
    assert "--resume" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--resume") + 1] == "S1"
    # The latest user message is the positional prompt, not the flattened history.
    assert "again" in captured["argv"]


@pytest.mark.anyio
async def test_request_raises_on_incomplete_stream():
    model = ClaudeCliModel("sonnet")
    model.spawn = _fake_objs(
        [{"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}]
    )
    from pydantic_ai.models import ModelRequestParameters

    with pytest.raises(CliModelError):
        await model.request(_user("hi"), None, ModelRequestParameters())


@pytest.mark.anyio
async def test_request_stream_yields_text_events():
    from pydantic_ai.messages import PartDeltaEvent, PartStartEvent
    from pydantic_ai.models import ModelRequestParameters

    model = ClaudeCliModel("sonnet")
    model.spawn = _fake_objs(
        [
            _INIT,
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "streamed"}]}},
            _result("streamed"),
        ]
    )
    events = []
    async with model.request_stream(_user("hi"), None, ModelRequestParameters()) as stream:
        async for ev in stream:
            events.append(ev)
        final = stream.get()
    assert any(isinstance(e, (PartStartEvent, PartDeltaEvent)) for e in events)
    assert final.parts[0].content == "streamed"
    assert model.session_id == "S1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_claude_cli_model.py -k "request" -v`
Expected: FAIL with `ImportError: cannot import name 'ClaudeCliModel'`

- [ ] **Step 3: Add the model + streamed response**

Append to `src/marim_harness/config/claude_cli_model.py`. Add `asyncio`, `json`, `logging`, `datetime` imports to the top block.

```python
import asyncio
import json
import logging
from datetime import datetime, timezone

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from pydantic_ai.settings import ModelSettings

logger = logging.getLogger(__name__)

_ask_noticed = False


def note_ask_limitation_once(mode: str) -> None:
    """Warn once per process that ``ask`` can't do per-tool gating in this provider
    (it is treated like ``auto``). Kept out of the pure mapping so tests stay quiet."""
    global _ask_noticed
    if mode == "ask" and not _ask_noticed:
        _ask_noticed = True
        logger.warning(
            "claude-cli provider: 'ask' mode cannot gate individual tools "
            "(headless claude can't prompt) — running like 'auto' (acceptEdits)."
        )


class CliModelError(Exception):
    """The claude CLI was unavailable or produced no terminal result."""


async def spawn_cli_objects(argv: list[str], cwd: str) -> AsyncIterator[dict]:
    """Spawn ``claude`` and yield each stream-json line as a parsed dict. Reaps the
    child on exit; drains stderr to avoid a pipe-buffer deadlock. Non-JSON noise is
    skipped. This is the only I/O seam — tests replace it via ``model.spawn``."""
    from ..subagents.cli_backend import _iter_ndjson_lines

    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_task = asyncio.ensure_future(proc.stderr.read()) if proc.stderr is not None else None
    try:
        assert proc.stdout is not None
        async for raw in _iter_ndjson_lines(proc.stdout):
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
        if stderr_task is not None:
            await stderr_task
            stderr_task = None
        await proc.wait()
    finally:
        if stderr_task is not None:
            stderr_task.cancel()
        if proc.returncode is None:
            import contextlib

            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(BaseException):
                await proc.wait()


class ClaudeCliModel(Model):
    """A Pydantic AI model backed by the ``claude`` CLI (a Claude subscription).

    Each request spawns ``claude -p`` (resuming Claude's session when one is known)
    and returns a single text-only ``ModelResponse``; Claude runs its own tools
    internally. ``mode_getter`` is set by bootstrap to read marim's live approval
    mode; ``session_id`` is held in-memory across turns of one process."""

    def __init__(self, model_id: str | None) -> None:
        super().__init__()
        self._model_id = model_id
        self.mode_getter: Callable[[], str] | None = None
        self.session_id: str | None = None
        self.spawn = spawn_cli_objects  # I/O seam; tests monkeypatch this
        self._ts = datetime.now(tz=timezone.utc)

    @property
    def model_name(self) -> str:
        return self._model_id or "default"

    @property
    def system(self) -> str:
        return "claude-cli"

    def _argv(self, messages: list) -> list[str]:
        from ..subagents.cli_backend import resolve_cli_binary
        from .claude_cli_model import build_cli_argv  # noqa: F401 (see below)

        binary = resolve_cli_binary()
        if binary is None:
            raise CliModelError(
                "claude CLI not found (set MARIM_CLAUDE_CLI_BIN or install Claude Code)."
            )
        mode = self.mode_getter() if self.mode_getter is not None else "plan"
        note_ask_limitation_once(mode)
        from ..subagents.cli_backend import build_cli_argv as _build

        if self.session_id:
            prompt, append_system = latest_user_text(messages), False
        else:
            # Cold turn: re-seed Claude with the whole conversation (resumed marim
            # session or first turn). For a brand-new session this is just the one
            # user message.
            prompt, append_system = flatten_history(messages), True
        return _build(
            binary,
            prompt,
            extract_system(messages),
            permission_mode_for(mode),
            [],  # let Claude use its own native toolset for the permission mode
            self._model_id,
            resume_session_id=self.session_id,
            append_system=append_system,
        )

    async def request(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        argv = self._argv(messages)
        done: DoneChunk | None = None
        async for chunk in consume_cli_stream(self.spawn(argv, ".")):
            if isinstance(chunk, DoneChunk):
                done = chunk
        if done is None or not done.complete:
            raise CliModelError("claude produced no result (crash or bad output).")
        if done.session_id:
            self.session_id = done.session_id
        return ModelResponse(
            parts=[TextPart(content=done.text)],
            model_name=self.model_name,
            timestamp=self._ts,
            usage=done.usage,
            provider_name="claude-cli",
        )

    async def request_stream(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context=None,
    ):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _cm() -> AsyncGenerator[StreamedResponse]:
            argv = self._argv(messages)
            stream = ClaudeCliStreamedResponse(
                model_request_parameters=model_request_parameters,
            )
            stream._objs = self.spawn(argv, ".")
            stream._model_id = self.model_name
            stream._ts = self._ts
            stream._set_session = lambda sid: setattr(self, "session_id", sid)
            yield stream

        return _cm()


class ClaudeCliStreamedResponse(StreamedResponse):
    """Streams ``consume_cli_stream`` output as text-delta events. Stores Claude's
    session id back on the model when the terminal chunk arrives."""

    _objs: AsyncIterator = None  # type: ignore[assignment]
    _model_id: str = "default"
    _ts: datetime = None  # type: ignore[assignment]
    _set_session = None

    async def _get_event_iterator(self):
        async for chunk in consume_cli_stream(self._objs):
            if isinstance(chunk, TextChunk):
                for event in self._parts_manager.handle_text_delta(
                    vendor_part_id="content", content=chunk.delta
                ):
                    yield event
            elif isinstance(chunk, DoneChunk):
                self._usage = chunk.usage
                if chunk.session_id and self._set_session is not None:
                    self._set_session(chunk.session_id)
                self._finished = True

    @property
    def model_name(self) -> str:
        return self._model_id

    @property
    def timestamp(self) -> datetime:
        return self._ts or datetime.now(tz=timezone.utc)

    @property
    def provider_name(self) -> str:
        return "claude-cli"

    @property
    def provider_url(self) -> str:
        return "https://claude.com/claude-code"
```

Cleanups while writing: remove the stray `from .claude_cli_model import build_cli_argv` placeholder line (it was illustrative) — the real import is `from ..subagents.cli_backend import build_cli_argv as _build`. Keep only that one. Make sure `request_stream` is **not** decorated on the class with `@asynccontextmanager` directly (the base is already an async-context-manager protocol); returning the `_cm()` context manager matches the base contract used by `openrouter_cost._CostOpenRouterModel.request_stream`. Verify against that file.

`ClaudeCliStreamedResponse` declares class-level attribute defaults so the base `@dataclass`'s generated `__init__(model_request_parameters)` still works and we set the rest after construction. If pyright complains about assigning to dataclass-managed slots, convert the class to `@dataclass` with those as `field(default=...)` after `model_request_parameters`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_claude_cli_model.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check --fix src/marim_harness/config/claude_cli_model.py tests/test_claude_cli_model.py && uv run pyright src/marim_harness/config/claude_cli_model.py`
Expected: no errors. If pyright flags the streamed-response attribute assignments, switch that class to a `@dataclass` as noted.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/config/claude_cli_model.py tests/test_claude_cli_model.py
git commit -m "feat: ClaudeCliModel request/request_stream over claude -p"
```

---

### Task 5: Register the `claude-cli` provider in config

**Files:**
- Modify: `src/marim_harness/config/model.py` (`_KNOWN_PROVIDERS:25`, `_provider_config:136`, `_provider_has_creds:165`, `build_model:255`, `ModelSource.list_models:295`)
- Test: `tests/test_config.py` (add cases; match the existing config test file name — `grep -rln "MARIM_PROVIDER\|_provider_config\|detect_active_providers" tests`)

**Interfaces:**
- Consumes: `ClaudeCliModel` (Task 4), `resolve_cli_binary` (`subagents.cli_backend`).
- Produces: `MARIM_PROVIDER=claude-cli` routes to a `ModelConfig(provider="claude-cli", model=<MARIM_MODEL or None>)`; `build_model` returns a `ClaudeCliModel`; `_provider_has_creds("claude-cli")` is True iff the binary resolves.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py  (add; adjust import to the module's real path)
from marim_harness.config import model as model_mod


def test_claude_cli_is_a_known_provider():
    assert "claude-cli" in model_mod._KNOWN_PROVIDERS


def test_provider_config_claude_cli(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "claude-cli")
    monkeypatch.delenv("MARIM_MODEL", raising=False)
    cfg = model_mod.load_config()
    assert cfg.provider == "claude-cli"
    assert cfg.model is None or isinstance(cfg.model, str)
    assert cfg.api_key is None
    assert cfg.base_url is None


def test_provider_config_claude_cli_model_override(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "claude-cli")
    monkeypatch.setenv("MARIM_MODEL", "opus")
    assert model_mod.load_config().model == "opus"


def test_has_creds_follows_binary(monkeypatch):
    monkeypatch.setattr(model_mod, "_claude_cli_available", lambda: True)
    assert model_mod._provider_has_creds("claude-cli") is True
    monkeypatch.setattr(model_mod, "_claude_cli_available", lambda: False)
    assert model_mod._provider_has_creds("claude-cli") is False


def test_build_model_claude_cli(monkeypatch):
    from dataclasses import replace

    cfg = replace(model_mod.load_config(), provider="claude-cli", model="sonnet")
    m = model_mod.build_model(cfg)
    from marim_harness.config.claude_cli_model import ClaudeCliModel

    assert isinstance(m, ClaudeCliModel)
    assert m.model_name == "sonnet"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config.py -k "claude_cli or claude-cli or has_creds" -v`
Expected: FAIL (`'claude-cli' not in _KNOWN_PROVIDERS`)

- [ ] **Step 3: Edit `config/model.py`**

(a) Add the default + known-provider entry near `_DEFAULT_GOOGLE_MODEL` (line ~20) and `_KNOWN_PROVIDERS` (line 25):

```python
_DEFAULT_GOOGLE_MODEL = "gemini-2.5-flash"
# None ⇒ let the claude CLI use its own configured default model.
_DEFAULT_CLAUDE_CLI_MODEL: str | None = None

_KNOWN_PROVIDERS = frozenset({"openrouter", "local", "google", "claude-cli"})
```

(b) Add a small availability helper (kept as a module function so tests monkeypatch it) above `_provider_has_creds` (line ~165):

```python
def _claude_cli_available() -> bool:
    """True when a ``claude`` binary can be resolved (the only 'cred' this provider
    needs; a not-logged-in CLI fails clearly at first use)."""
    from ..subagents.cli_backend import resolve_cli_binary

    return resolve_cli_binary() is not None
```

(c) Add a `claude-cli` branch in `_provider_config` (before the final openrouter fallback, after the `google` branch at line 155):

```python
    if provider == "claude-cli":
        return ModelConfig(
            provider="claude-cli",
            model=os.getenv("MARIM_MODEL", _DEFAULT_CLAUDE_CLI_MODEL),
            base_url=None,
            api_key=None,  # the CLI owns auth (the Claude subscription)
            **common,
        )
```

Note: `ModelConfig.model` is typed `str`. Change it to `str | None` at the dataclass field (line 56: `model: str`) → `model: str | None`, since the claude-cli default is `None` (let the CLI choose). Verify no other branch relies on `model` being non-None at construction (the other three always set a string).

(d) Add a `claude-cli` branch in `_provider_has_creds` (line 165):

```python
    if provider == "claude-cli":
        return _claude_cli_available()
```

(e) Add a `claude-cli` branch in `build_model` (line 255, before the openrouter fallback import):

```python
    if cfg.provider == "claude-cli":
        from .claude_cli_model import ClaudeCliModel

        return ClaudeCliModel(cfg.model)
```

(f) `ModelSource.list_models` (line 295): add a static catalog so the picker shows entries. Add before the final `return []`:

```python
        if self.cfg.provider == "claude-cli":
            from .catalog import ModelEntry

            return [
                ModelEntry(id="sonnet", provider="claude-cli"),
                ModelEntry(id="opus", provider="claude-cli"),
                ModelEntry(id="haiku", provider="claude-cli"),
            ]
```

Check `ModelEntry`'s required fields first (`uv run python -c "from marim_harness.workspace.catalog import ModelEntry; import dataclasses; print([f.name for f in dataclasses.fields(ModelEntry)])"`) and fill any other required field with a sensible constant (e.g. an empty/`None` description). The import path for `ModelEntry` is `..workspace.catalog` as used at the top of `model.py` — match that exact import, not `.catalog`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -k "claude_cli or claude-cli or has_creds" -v`
Expected: PASS

- [ ] **Step 5: Full config + model test sweep (no regressions)**

Run: `uv run pytest tests/test_config.py tests/test_claude_cli_model.py -v`
Expected: PASS

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check --fix src/marim_harness/config/model.py && uv run pyright src/marim_harness/config/model.py`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/config/model.py tests/test_config.py
git commit -m "feat: register claude-cli as a selectable model provider"
```

---

### Task 6: Bootstrap wiring, ask-mode notice, and docs

**Files:**
- Modify: `src/marim_harness/runtime/bootstrap.py` (`build_harness`, after the `Harness` is constructed)
- Modify: `.env.example`
- Modify: `CLAUDE.md` (the provider list under "Commands")
- Test: `tests/test_bootstrap.py` (add a case; match the real file — `grep -rln "build_harness" tests`)

**Interfaces:**
- Consumes: `ClaudeCliModel` (Task 4); `Harness.mode` property (`runtime/harness.py:423`).
- Produces: after `build_harness`, if the active model is a `ClaudeCliModel`, its `mode_getter` returns `harness.mode.value` so per-turn mode mapping reflects live `/mode` switches.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bootstrap.py  (add)
from pathlib import Path

import pytest


def test_build_harness_wires_claude_cli_mode_getter(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "claude-cli")
    monkeypatch.delenv("MARIM_MODEL", raising=False)
    # Pretend the binary exists so the provider is selected and built.
    from marim_harness.config import model as model_mod

    monkeypatch.setattr(model_mod, "_claude_cli_available", lambda: True)
    monkeypatch.setattr(
        "marim_harness.subagents.cli_backend.resolve_cli_binary", lambda: "/usr/bin/claude"
    )

    from marim_harness.runtime.bootstrap import build_harness
    from marim_harness.config.claude_cli_model import ClaudeCliModel

    harness = build_harness(Path(tmp_path))
    assert isinstance(harness.current_model, ClaudeCliModel)
    assert harness.current_model.mode_getter is not None
    # The getter reflects the harness's live mode as a plain string.
    assert harness.current_model.mode_getter() == harness.mode.value
```

If `build_harness` has required args beyond `workspace` or does heavy I/O that's awkward in a unit test, mirror the setup used by the nearest existing `build_harness` test in that file (grep first). Skip with `pytest.importorskip`/a guard only if the existing tests do.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_bootstrap.py -k "claude_cli" -v`
Expected: FAIL (`mode_getter is None`)

- [ ] **Step 3: Wire it in `build_harness`**

After the `Harness(...)` instance is constructed and before it is returned (find the `harness = Harness(` / `return harness` lines in `runtime/bootstrap.py`), add:

```python
    # The claude-cli provider needs marim's *live* approval mode each turn (to pick
    # Claude's --permission-mode) — bind it the same late way `get_model` is bound,
    # so a runtime /mode switch is honored on the next turn.
    from ..config.claude_cli_model import ClaudeCliModel

    if isinstance(harness.current_model, ClaudeCliModel):
        harness.current_model.mode_getter = lambda: harness.mode.value
```

If a `/model` switch can later build a `ClaudeCliModel` at runtime (via `MultiModelSource.build`), the same binding belongs wherever `current_model` is reassigned. Check `runtime/harness.py:412-417` (`switch_model`): if it sets `self.current_model = model`, add there as well:

```python
        if isinstance(model, ClaudeCliModel):
            model.mode_getter = lambda: self.mode.value
```

with a local `from ..config.claude_cli_model import ClaudeCliModel` import inside the method.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_bootstrap.py -k "claude_cli" -v`
Expected: PASS

- [ ] **Step 5: Update docs**

In `.env.example`, add `claude-cli` to the `MARIM_PROVIDER` options and document the model knob. Add near the existing provider lines:

```bash
# Use a Claude Pro/Max subscription via the `claude` CLI (Claude Code must be
# installed and logged in). In this provider marim delegates the whole turn to
# `claude -p`: Claude runs its own tools/loop, so marim's own tools, approval
# gating, LSP, and MCP do not apply. `auto`/`ask` map to acceptEdits, `plan` to
# read-only plan. MARIM_MODEL is optional (sonnet/opus/haiku; default: CLI's own).
# MARIM_PROVIDER=claude-cli
# MARIM_MODEL=sonnet
# MARIM_CLAUDE_CLI_BIN=claude   # override the binary if not on PATH
```

In `CLAUDE.md`, update the provider enumeration (the "Provider config lives in env vars" paragraph in the Commands section) from `(openrouter|local|google)` to `(openrouter|local|google|claude-cli)` and add one sentence: "`claude-cli` delegates each turn to the `claude` CLI on a Claude subscription — marim acts as a launcher (Claude runs its own tools/loop), so marim's own tools/approval/LSP/MCP do not apply in that provider."

- [ ] **Step 6: Full suite, lint, type-check (CI parity)**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all green. Coverage is on by default; the new module's pure helpers and stream consumer are covered. If coverage gates fail only on the I/O `spawn_cli_objects` body, that's expected (it's the integration seam) — confirm the threshold still passes; if not, add `# pragma: no cover` to the subprocess-only lines inside `spawn_cli_objects`.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/runtime/bootstrap.py src/marim_harness/runtime/harness.py .env.example CLAUDE.md tests/test_bootstrap.py
git commit -m "feat: wire claude-cli mode_getter in bootstrap; document the provider"
```

---

## Self-Review

**1. Spec coverage:**
- Custom `Model` at the `build_model` seam, no turn-loop change → Tasks 4, 5, 6. ✓
- Pure-text response invariant (no `ToolCallPart`s) → Task 4 (`request` builds a single `TextPart`; test `test_request_returns_text_only_response_and_captures_session` asserts it). ✓
- Reuse `cli_backend` pure helpers (`resolve_cli_binary`, `build_cli_argv`, `_iter_ndjson_lines`, cache-folding usage) → Tasks 2, 3, 4. ✓
- Approval mapping `auto`/`ask`→acceptEdits, `plan`→plan, one-time `ask` notice → Task 1 (`permission_mode_for`) + Task 4 (`note_ask_limitation_once`). ✓
- History via `--resume <session_id>` with cold-start flatten fallback → Task 1 (`flatten_history`/`latest_user_text`), Task 2 (`--resume` argv), Task 4 (`_argv` chooses resume vs flatten; `test_second_turn_uses_resume`). Note: spec's "persist session_id on the marim session" is deliberately simplified to in-memory + flatten-on-cold-start (documented in the plan header and Task 4) — strictly simpler, equally correct; cross-process resume re-seeds via flatten. ✓
- Streaming text + folded activity log → Task 3 (`format_activity_line`, `consume_cli_stream`) + Task 4 (`request_stream`/`ClaudeCliStreamedResponse`). ✓
- Tools not mapped (`--allowedTools` omitted; Claude uses native toolset) → Task 4 (`_argv` passes `[]`). ✓
- Usage & cost via `COST_DETAIL_KEY` → Task 1 (`request_usage_from_cli`). ✓
- Error handling (missing binary, no result, resume failure) → Task 4 (`CliModelError`; `test_request_raises_on_incomplete_stream`). Resume-failure auto-fallback: the cold-start path covers a *fresh process*; a mid-session `--resume` that errors surfaces as `CliModelError` (the turn ends cleanly, history intact) rather than auto-retrying — a conscious v1 narrowing of the spec's "automatic one-turn flatten fallback." Documented here; revisit if it bites. ✓
- `list_models` static catalog → Task 5(f). ✓
- Docs (`.env.example`, `CLAUDE.md`) → Task 6. ✓
- Out-of-scope items (canUseTool brokering, structured nested rendering, MCP mapping, rewind reconciliation) → not implemented, by design. ✓

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to" — every code step shows complete code. The one illustrative bad import line in Task 4 Step 3 is explicitly called out and corrected in the same step's cleanup note. ✓

**3. Type consistency:** `ClaudeCliModel` attrs (`mode_getter`, `session_id`, `spawn`, `model_name`), `consume_cli_stream`/`TextChunk`/`DoneChunk`, `permission_mode_for`/`extract_system`/`latest_user_text`/`flatten_history`/`request_usage_from_cli`, and `build_cli_argv(..., resume_session_id=, append_system=)` are named identically across the tasks that define and consume them. `mode_getter()` returns a `str` (`harness.mode.value`) and `permission_mode_for` consumes a `str` — consistent. `ModelConfig.model` widened to `str | None` in Task 5 to carry the `None` default. ✓

## Known verification gaps (call out at execution time)

- `request_stream`'s exact contract (returning a context manager vs. being decorated) must be confirmed against `openrouter_cost._CostOpenRouterModel.request_stream` and pydantic_ai 1.107 — Task 4 Step 3 says to verify; do it before assuming the test passes.
- The async test marker (`anyio` vs `asyncio` vs `asyncio.run`) must match the repo's existing convention — Task 3 Step 1 says to grep and match.
- A real end-to-end turn against an installed, logged-in `claude` is **not** unit-tested (the I/O seam is monkeypatched). Add the opt-in smoke test from the spec ("Testing") behind an env guard if you want live coverage; otherwise verify manually: `MARIM_PROVIDER=claude-cli uv run marim` and run one prompt.
```
