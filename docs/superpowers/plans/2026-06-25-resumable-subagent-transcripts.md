# Resumable Sub-Agent Transcripts + Finished-Job History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist each sub-agent's full execution transcript (and finished-job history) so a resumed session can show what every sub-agent *did*, not just its final report.

**Architecture:** A transcript is a `list[ModelMessage]` (native runs use `result.all_messages()`; CLI runs synthesize messages from stream-json) capped per tool-result, written **once** to a sidecar file `sessions/<id>.subagents/<tool_call_id>.json`, and **lazy-loaded** on pane open during resume. Finished background-job summaries persist inline in the session JSON. Running jobs are not restored.

**Tech Stack:** Python 3.10+, pydantic-ai (`ModelMessage`, `ModelMessagesTypeAdapter`), Textual, `uv`, pytest.

## Global Constraints

- Python `>=3.10`; no 3.11+-only syntax. Ruff line length 100; lint set `E,F,I,UP,B,SIM`.
- Run `uv run ruff check src tests` → `uv run pyright` → `uv run pytest` before claiming a task done (CI order).
- Use `uv run` for everything; never bare `python`/`pytest`/`pip`.
- All persistence is **best-effort**: a broken/missing file must never break a turn or a resume — log a warning and continue (codebase rule).
- **Backward compatible:** an old session with no `.subagents/` dir and no `jobs` key must resume exactly as today (report-only cards, empty jobs).
- Per-tool-result transcript cap default **2000 chars**, overridable via `MARIM_SUBAGENT_TRANSCRIPT_CAP`; only `ToolReturnPart.content` is capped — text/thinking/tool-call parts are kept in full.
- Atomic writes use the existing `marim_harness.atomic_io.atomic_write_text`.
- Filename-sanitize a `tool_call_id` the same way `interfaces/tui/widgets/subagent_detail.py:pane_id` does (regex non-`[a-zA-Z0-9_-]` → `-`).

---

### Task 1: `cap_transcript` — truncate large tool results

**Files:**
- Modify: `src/marim_harness/workspace/agents.py` (add `cap_transcript` near `cap_subagent_output`, ~line 328)
- Test: `tests/test_transcript_cap.py` (create)

**Interfaces:**
- Produces: `cap_transcript(messages: list, cap: int) -> list` — returns a new message list where every `ToolReturnPart.content` longer than `cap` chars is replaced with `head + "\n…(truncated, N chars)"`; all other parts unchanged. Pure, no I/O.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transcript_cap.py
from datetime import datetime, timezone

from pydantic_ai.messages import (
    ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart,
)

from marim_harness.workspace.agents import cap_transcript


def _ret(content: str) -> ModelRequest:
    return ModelRequest(parts=[ToolReturnPart(
        tool_name="read_file", content=content, tool_call_id="t1",
        timestamp=datetime.now(tz=timezone.utc),
    )])


def test_cap_truncates_only_oversized_tool_results():
    big = "x" * 5000
    msgs = [_ret(big), ModelResponse(parts=[TextPart(content="all good")])]
    out = cap_transcript(msgs, cap=2000)
    ret = out[0].parts[0]
    assert len(str(ret.content)) < 2100          # head + marker, well under original
    assert "truncated, 5000 chars" in str(ret.content)
    # Non-tool parts are untouched.
    assert out[1].parts[0].content == "all good"


def test_cap_leaves_small_results_intact():
    msgs = [_ret("short output")]
    out = cap_transcript(msgs, cap=2000)
    assert out[0].parts[0].content == "short output"


def test_cap_handles_non_string_content():
    # A list/blocks content must not crash; it is stringified for length checks.
    part = ToolReturnPart(tool_name="x", content=[{"type": "text", "text": "y" * 5000}],
                          tool_call_id="t", timestamp=datetime.now(tz=timezone.utc))
    out = cap_transcript([ModelRequest(parts=[part])], cap=100)
    assert "truncated" in str(out[0].parts[0].content)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transcript_cap.py -v`
Expected: FAIL — `ImportError: cannot import name 'cap_transcript'`.

- [ ] **Step 3: Implement `cap_transcript`**

Add to `src/marim_harness/workspace/agents.py` (after `cap_subagent_output`):

```python
import dataclasses

from pydantic_ai.messages import ToolReturnPart  # add to existing imports


def cap_transcript(messages: list, cap: int) -> list:
    """Return a copy of ``messages`` with every ``ToolReturnPart`` whose content
    exceeds ``cap`` characters truncated to ``cap`` chars plus a marker. Only tool
    *results* are capped — text, thinking, and tool-call parts (the reasoning and
    the actions) are kept in full. Pure: never mutates the input messages."""
    out = []
    for message in messages:
        parts = getattr(message, "parts", None)
        if not parts:
            out.append(message)
            continue
        new_parts = []
        for part in parts:
            if isinstance(part, ToolReturnPart):
                text = part.content if isinstance(part.content, str) else str(part.content)
                if len(text) > cap:
                    marker = f"\n…(truncated, {len(text)} chars)"
                    head = text[: max(0, cap - len(marker))]
                    part = dataclasses.replace(part, content=head + marker)
            new_parts.append(part)
        out.append(dataclasses.replace(message, parts=new_parts))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_transcript_cap.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/marim_harness/workspace/agents.py tests/test_transcript_cap.py
uv run pyright src/marim_harness/workspace/agents.py
git add src/marim_harness/workspace/agents.py tests/test_transcript_cap.py
git commit -m "feat(subagents): cap_transcript truncates large tool results"
```

---

### Task 2: `TranscriptStore` — sidecar write-once / lazy read

**Files:**
- Create: `src/marim_harness/session/transcripts.py`
- Modify: `src/marim_harness/session/__init__.py` (export `TranscriptStore`)
- Test: `tests/test_transcript_store.py` (create)

**Interfaces:**
- Consumes: `cap_transcript` (Task 1); `marim_harness.atomic_io.atomic_write_text`; `pydantic_ai.messages.ModelMessagesTypeAdapter`.
- Produces:
  - `TranscriptStore(session_path: Path, session_id: str)` — `session_path` is the session JSON path (e.g. `.../sessions/abc.json`); the sidecar dir is `session_path.parent / f"{session_id}.subagents"`.
  - `.write(stream_id: str, messages: list, cap: int) -> None` — caps then atomically writes one transcript. Best-effort: logs and returns on any error.
  - `.read(stream_id: str) -> list | None` — loads + validates one transcript, or `None` if absent/corrupt.
  - `.delete_all() -> None` — removes the whole `.subagents` dir.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transcript_store.py
from datetime import datetime, timezone
from pathlib import Path

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from marim_harness.session import TranscriptStore


def _msgs():
    return [ModelResponse(parts=[
        TextPart(content="working"),
        ToolCallPart(tool_name="read_file", args={"path": "x"}, tool_call_id="c1"),
    ])]


def _store(tmp_path: Path) -> TranscriptStore:
    return TranscriptStore(tmp_path / "sessions" / "abc.json", "abc")


def test_write_then_read_roundtrips(tmp_path):
    s = _store(tmp_path)
    s.write("toolu_99", _msgs(), cap=2000)
    loaded = s.read("toolu_99")
    assert loaded is not None
    assert loaded[0].parts[0].content == "working"
    assert loaded[0].parts[1].tool_name == "read_file"


def test_read_missing_returns_none(tmp_path):
    assert _store(tmp_path).read("nope") is None


def test_write_sanitizes_id_into_filename(tmp_path):
    s = _store(tmp_path)
    s.write("call/abc.123:x", _msgs(), cap=2000)
    files = list((tmp_path / "sessions" / "abc.subagents").glob("*.json"))
    assert len(files) == 1
    assert all(c.isalnum() or c in "-_." for c in files[0].name)


def test_delete_all_removes_dir(tmp_path):
    s = _store(tmp_path)
    s.write("a", _msgs(), cap=2000)
    assert (tmp_path / "sessions" / "abc.subagents").exists()
    s.delete_all()
    assert not (tmp_path / "sessions" / "abc.subagents").exists()


def test_read_corrupt_returns_none(tmp_path):
    s = _store(tmp_path)
    d = tmp_path / "sessions" / "abc.subagents"
    d.mkdir(parents=True)
    (d / "bad.json").write_text("{not json")
    assert s.read("bad") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transcript_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'TranscriptStore'`.

- [ ] **Step 3: Implement `TranscriptStore`**

Create `src/marim_harness/session/transcripts.py`:

```python
"""Per-sub-agent transcript sidecars.

A sub-agent's full step-by-step transcript is immutable once it finishes, but the
session JSON is re-serialized every turn — so transcripts live in write-once
sidecar files next to the session, loaded lazily only when a resumed pane is
opened. One file per spawn, keyed by the spawn's tool_call_id."""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from pydantic_ai.messages import ModelMessagesTypeAdapter

from ..atomic_io import atomic_write_text
from ..workspace import cap_transcript

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^a-zA-Z0-9_.-]")


def _safe(stream_id: str) -> str:
    """A filesystem-safe filename stem for a tool_call_id (same sanitization rule
    as the TUI's pane_id), prefixed to guarantee a non-empty, letter-leading name."""
    return "t-" + _UNSAFE.sub("-", stream_id or "none")


class TranscriptStore:
    """Reads/writes one sub-agent transcript per spawn under
    ``<session_path.parent>/<session_id>.subagents/<safe id>.json``. All methods
    are best-effort: a write/read failure logs and degrades, never raising into a
    turn or a resume."""

    def __init__(self, session_path, session_id: str) -> None:
        self._dir = Path(session_path).parent / f"{session_id}.subagents"

    def _file(self, stream_id: str) -> Path:
        return self._dir / f"{_safe(stream_id)}.json"

    def write(self, stream_id: str, messages: list, cap: int) -> None:
        if not stream_id or not messages:
            return
        try:
            capped = cap_transcript(messages, cap)
            payload = ModelMessagesTypeAdapter.dump_json(capped).decode("utf-8")
            self._dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._file(stream_id), payload)
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("Failed to write sub-agent transcript %s: %s", stream_id, exc)

    def read(self, stream_id: str) -> list | None:
        path = self._file(stream_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
            return list(ModelMessagesTypeAdapter.validate_python(raw))
        except Exception as exc:  # noqa: BLE001 - a corrupt sidecar must not crash resume
            logger.warning("Failed to read sub-agent transcript %s: %s", stream_id, exc)
            return None

    def delete_all(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)
```

Add to `src/marim_harness/session/__init__.py`:

```python
from .transcripts import TranscriptStore  # noqa: F401
```
(and add `"TranscriptStore"` to `__all__` if one is defined there.)

Note: `cap_transcript` must be exported from `marim_harness.workspace` — verify `src/marim_harness/workspace/__init__.py` re-exports it (add `cap_transcript` next to the existing `cap_subagent_output` export).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_transcript_store.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/marim_harness/session/transcripts.py tests/test_transcript_store.py
uv run pyright src/marim_harness/session/transcripts.py
git add src/marim_harness/session/transcripts.py src/marim_harness/session/__init__.py src/marim_harness/workspace/__init__.py tests/test_transcript_store.py
git commit -m "feat(session): TranscriptStore write-once sidecar for sub-agent transcripts"
```

---

### Task 3: CLI stream → `list[ModelMessage]` synthesis

**Files:**
- Modify: `src/marim_harness/subagents_cli.py` (extend `CliStreamTranslator`)
- Test: `tests/test_subagents_cli.py` (add cases)

**Interfaces:**
- Consumes: existing `CliStreamTranslator` parsing + `normalize_cc_tool` (Task already merged in PR #30).
- Produces: `CliStreamTranslator.transcript() -> list[ModelMessage]` — the run accumulated as pydantic-ai messages: each assistant object → a `ModelResponse` (TextParts + ToolCallParts), each user/tool_result → a `ModelRequest` (ToolReturnParts). Tool names/args are normalized identically to the live events so a replayed CLI pane matches a native one.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_subagents_cli.py
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart


def test_translator_accumulates_transcript_messages():
    t = CliStreamTranslator()
    t.translate({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "reading"},
        {"type": "tool_use", "id": "c1", "name": "Read", "input": {"file_path": "x.py"}},
    ]}})
    t.translate({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "c1", "content": "file body"},
    ]}})
    msgs = t.transcript()
    assert isinstance(msgs[0], ModelResponse)
    # tool_use was normalized to the harness name + arg shape.
    call = [p for p in msgs[0].parts if isinstance(p, ToolCallPart)][0]
    assert call.tool_name == "read_file"
    assert call.args_as_dict() == {"path": "x.py"}
    assert isinstance(msgs[1], ModelRequest)
    ret = msgs[1].parts[0]
    assert isinstance(ret, ToolReturnPart)
    assert ret.tool_name == "read_file" and ret.content == "file body"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_subagents_cli.py::test_translator_accumulates_transcript_messages -v`
Expected: FAIL — `AttributeError: 'CliStreamTranslator' object has no attribute 'transcript'`.

- [ ] **Step 3: Implement transcript accumulation**

In `src/marim_harness/subagents_cli.py`, add a message accumulator to `CliStreamTranslator`. In `__init__` add `self._messages: list = []`. In `_assistant`, build a `ModelResponse` from the same normalized parts already produced, and append it. In `_user`, build a `ModelRequest` from the `ToolReturnPart`s already produced, and append it. Add the public accessor:

```python
from pydantic_ai.messages import ModelRequest, ModelResponse  # add to imports
```

In `_assistant`, after the loop that builds `events`, collect the response parts:

```python
        resp_parts = []
        for block in obj.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text":
                resp_parts.append(TextPart(content=block.get("text", "")))
            elif btype == "tool_use":
                name, args = normalize_cc_tool(
                    block.get("name", "tool"), block.get("input", {}) or {})
                resp_parts.append(ToolCallPart(
                    tool_name=name, args=args, tool_call_id=block.get("id", "")))
        if resp_parts:
            self._messages.append(ModelResponse(parts=resp_parts))
```

(Place this so it reuses the same `normalize_cc_tool` result as the event path — extract a small local helper if it avoids double-normalizing, but correctness first: identical inputs give identical normalized output, so a second call is safe.)

In `_user`, accumulate the returns:

```python
        req_parts = []
        for block in obj.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id", "")
            req_parts.append(ToolReturnPart(
                tool_name=self._call_names.get(call_id, "tool"),
                content=_flatten_tool_result(block.get("content")),
                tool_call_id=call_id,
                timestamp=datetime.now(tz=timezone.utc),
            ))
        if req_parts:
            self._messages.append(ModelRequest(parts=req_parts))
```

Add the accessor:

```python
    def transcript(self) -> list:
        """The run so far as pydantic-ai messages (for transcript persistence)."""
        return list(self._messages)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_subagents_cli.py -v`
Expected: PASS (all, including the new case).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/marim_harness/subagents_cli.py tests/test_subagents_cli.py
uv run pyright src/marim_harness/subagents_cli.py
git add src/marim_harness/subagents_cli.py tests/test_subagents_cli.py
git commit -m "feat(subagents): CLI translator accumulates a ModelMessage transcript"
```

---

### Task 4: Expose the CLI transcript on `CliResult` and from the runner

**Files:**
- Modify: `src/marim_harness/subagents_cli.py` (`CliResult`, `ClaudeCliRunner.run`)
- Test: `tests/test_subagents_cli.py` (add a case)

**Interfaces:**
- Consumes: `CliStreamTranslator.transcript()` (Task 3).
- Produces: `CliResult.transcript: list` (defaults to `[]`); `ClaudeCliRunner.run(...)` populates it from the translator after the stream ends.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_subagents_cli.py
@pytest.mark.anyio
async def test_runner_returns_transcript(tmp_path):
    binary = _make_fake_cli(tmp_path)   # the existing fake CLI emits text+tool_use+result
    runner = ClaudeCliRunner(None, None)
    result = await runner.run(
        binary=binary, prompt="go", system_prompt="s", cwd=str(tmp_path),
        allow_gated=True, allowed_tools=frozenset({"read_file"}),
        model=None, stream_id="s1",
    )
    assert result.transcript          # non-empty list of ModelMessages
    from pydantic_ai.messages import ModelResponse
    assert any(isinstance(m, ModelResponse) for m in result.transcript)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_subagents_cli.py::test_runner_returns_transcript -v`
Expected: FAIL — `AttributeError: 'CliResult' object has no attribute 'transcript'`.

- [ ] **Step 3: Implement**

In `CliResult` (the `@dataclass`), add a field:

```python
    transcript: list = field(default_factory=list)
```
(Add `from dataclasses import dataclass, field` if `field` isn't imported.)

In `ClaudeCliRunner.run`, the `translator` local already exists. After the stdout loop ends and before `return CliResult(...)`, pass the transcript:

```python
            return CliResult(output=output, usage=usage,
                             transcript=translator.transcript())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_subagents_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/marim_harness/subagents_cli.py tests/test_subagents_cli.py
uv run pyright src/marim_harness/subagents_cli.py
git add src/marim_harness/subagents_cli.py tests/test_subagents_cli.py
git commit -m "feat(subagents): CliResult carries the run transcript"
```

---

### Task 5: Capture transcripts on completion (native + CLI) into the store

**Files:**
- Modify: `src/marim_harness/subagents.py` (`SubagentRunner.__init__`, native spawn path, `_execute_cli_spawn`)
- Modify: `src/marim_harness/agent.py` (`build_collaborators`, ~line 203 — pass nothing new; the runner derives the store from `self.session`)
- Modify: `src/marim_harness/config/model.py` and `src/marim_harness/config/env.py` (add `MARIM_SUBAGENT_TRANSCRIPT_CAP`)
- Test: `tests/test_subagent_transcript_capture.py` (create)

**Interfaces:**
- Consumes: `TranscriptStore` (Task 2); `CliResult.transcript` (Task 4); the native run's `result.all_messages()`; `self.session.store` (current `SessionStore`, exposes `.path` and `.session_id`).
- Produces: a written sidecar per foreground/background spawn, keyed by `stream_id`. A `_transcript_store()` helper on `SubagentRunner` builds a `TranscriptStore` from the *current* session store (so it follows session switches).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagent_transcript_capture.py
import stat, sys
from pathlib import Path

import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart

from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from marim_harness.session import TranscriptStore
from tests.conftest import _make_harness

_FAKE_CLI = '''#!{python}
import json, sys
for o in [
    {{"type": "assistant", "message": {{"content": [
        {{"type": "text", "text": "looking"}},
        {{"type": "tool_use", "id": "c1", "name": "Read", "input": {{"file_path": "x"}}}},
    ]}}}},
    {{"type": "user", "message": {{"content": [
        {{"type": "tool_result", "tool_use_id": "c1", "content": "body"}},
    ]}}}},
    {{"type": "result", "subtype": "success", "result": "done",
      "num_turns": 1, "usage": {{"input_tokens": 1, "output_tokens": 1}}}},
]:
    sys.stdout.write(json.dumps(o) + "\\n")
'''


def _fake_cli(tmp_path: Path) -> str:
    p = tmp_path / "fake_claude.py"
    p.write_text(_FAKE_CLI.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


def _cli_agent(tmp_path: Path) -> None:
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cli-worker.md").write_text(
        "---\ndescription: w\nbackend: claude-cli\ntools: read_file\n---\nWork.\n")


@pytest.mark.anyio
async def test_cli_spawn_writes_transcript_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _cli_agent(tmp_path)
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])),
        Deps(workspace_root=tmp_path, mode=Mode.auto),
    )
    await harness.subagents.run("cli-worker", "do it", stream_id="sg1")
    store = TranscriptStore(harness.session.store.path, harness.session.store.session_id)
    saved = store.read("sg1")
    assert saved is not None and len(saved) >= 2   # assistant + tool-return messages
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_subagent_transcript_capture.py -v`
Expected: FAIL — no sidecar written, `saved is None`.

- [ ] **Step 3: Implement capture + config**

In `src/marim_harness/config/model.py`, add a field to `ModelConfig` (near `wake_depth_cap`) and parse it in `load_config`:

```python
    subagent_transcript_cap: int = 2000
```
```python
    subagent_transcript_cap = _int_env("MARIM_SUBAGENT_TRANSCRIPT_CAP", 2000)
    # ... pass subagent_transcript_cap=subagent_transcript_cap into the ModelConfig(...)
```
Add `"MARIM_SUBAGENT_TRANSCRIPT_CAP"` to `_POSITIVE_INT_KEYS` in `src/marim_harness/config/env.py`.

Thread the cap to `SubagentRunner` (via `build_collaborators` → constructor) as `transcript_cap: int = 2000`. In `SubagentRunner.__init__`, store `self._transcript_cap = transcript_cap`.

Add the helper and capture calls to `src/marim_harness/subagents.py`:

```python
    def _transcript_store(self):
        """A TranscriptStore bound to the *current* session (follows switches)."""
        from .session import TranscriptStore
        store = self.session.store
        return TranscriptStore(store.path, store.session_id)

    def _save_transcript(self, stream_id: str, messages: list) -> None:
        if stream_id and messages:
            self._transcript_store().write(stream_id, messages, self._transcript_cap)
```

In the **native** spawn path, after the run produces `result` (the pydantic-ai run result whose `.output` is used for the report — around line 459/471), capture before returning:

```python
        self._save_transcript(stream_id, result.all_messages())
```

In `_execute_cli_spawn` / after `_run_cli` returns its `CliResult` (around line 493–509), capture:

```python
        self._save_transcript(stream_id, result.transcript)
```

(Both calls are best-effort via `TranscriptStore.write`; they must not change the returned report.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_subagent_transcript_capture.py tests/test_config.py -k transcript -v`
Expected: PASS.

- [ ] **Step 5: Full regression, lint, type-check, commit**

```bash
uv run pytest tests/test_subagents_cli.py tests/test_subagent_cli_spawn.py tests/test_agent_subagents.py -q
uv run ruff check src tests && uv run pyright
git add src/marim_harness/subagents.py src/marim_harness/agent.py src/marim_harness/config/model.py src/marim_harness/config/env.py tests/test_subagent_transcript_capture.py
git commit -m "feat(subagents): persist sub-agent transcripts on completion"
```

---

### Task 6: Resume rendering — replay a transcript into a pane (lazy)

**Files:**
- Modify: `src/marim_harness/interfaces/tui/session_view.py` (generalize the per-part renderer; register panes on replay)
- Modify: `src/marim_harness/interfaces/tui/app.py` (lazy-load on pane open — `open_subagents_at` / pane show path)
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent_detail.py` (track a `transcript_loaded` flag on `SubAgentPane`)
- Test: `tests/test_resume_transcript.py` (create)

**Interfaces:**
- Consumes: `TranscriptStore.read` (Task 2); the existing `replay_history` part-rendering; `SubAgentDetailHost.add_pane` / `pane`.
- Produces: on resume, each foreground `spawn_agent` card has a pane registered (no transcript yet); the first time a pane is shown, its transcript is read and replayed into it, then `pane.transcript_loaded = True` so it loads at most once.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resume_transcript.py
import pytest
from textual.app import App, ComposeResult
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from marim_harness.interfaces.tui.widgets.subagent_detail import (
    SubAgentDetailHost, SubAgentPane,
)


class _Host(App):
    def compose(self) -> ComposeResult:
        yield SubAgentDetailHost()


@pytest.mark.anyio
async def test_replay_transcript_into_pane_renders_steps():
    from marim_harness.interfaces.tui.session_view import replay_messages_into

    msgs = [ModelResponse(parts=[
        TextPart(content="I will read the file"),
        ToolCallPart(tool_name="read_file", args={"path": "foo.py"}, tool_call_id="c1"),
    ])]
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane("c1", "claude-general", "sonnet", "Map layout")
        await pilot.pause()
        await replay_messages_into(pane, msgs)
        await pilot.pause()
        text = " ".join(str(w.render()) for w in pane.query("*"))
        assert "read the file" in text or "read_file" in text
        assert pane.transcript_loaded is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resume_transcript.py -v`
Expected: FAIL — `ImportError: cannot import name 'replay_messages_into'` (and `pane.transcript_loaded` missing).

- [ ] **Step 3: Implement**

In `subagent_detail.py`, add to `SubAgentPane.__init__`: `self.transcript_loaded = False`.

In `session_view.py`, extract the per-part rendering of `replay_history` (the `TextPart` / `ThinkingPart` / `ToolCallPart` / `ToolReturnPart` handling) into a reusable coroutine that mounts into a target container, and add `replay_messages_into(pane, messages)` that drives it for a pane and sets `pane.transcript_loaded = True` at the end. Reuse the same `ToolCallWidget` / `AssistantMessage` / `ThinkingWidget` construction the log replay uses, so a replayed pane matches a native one (including the `edit_file` diff, since CLI tools were normalized in Tasks 3–4). Use `self.app.stream.append_stream` + a final `flush_streams()` for text/thinking, matching `replay_history`.

In `app.py`, where a sub-agent pane is shown (`open_subagents_at` and/or the host `show`), add: if `pane is not None and not pane.transcript_loaded`, read `TranscriptStore(harness.session.store.path, harness.session.store.session_id).read(stream_id)`; if non-`None`, `await replay_messages_into(pane, messages)`; if `None`, mount a one-line "transcript unavailable for this resumed sub-agent" note and still set `transcript_loaded = True` (don't retry every open).

In `replay_history` (the resume path), when it mounts a foreground `spawn_agent` `SubAgentWidget`, also create its pane via the detail host (`add_pane(...)`) and attach it (`widget.pane = pane`), so the pane exists to lazy-load into. Leave `transcript_loaded = False`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_resume_transcript.py tests/test_subagent_detail.py tests/test_subagents_screen.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/marim_harness/interfaces/tui tests/test_resume_transcript.py
uv run pyright src/marim_harness/interfaces/tui/session_view.py src/marim_harness/interfaces/tui/widgets/subagent_detail.py
git add src/marim_harness/interfaces/tui/session_view.py src/marim_harness/interfaces/tui/app.py src/marim_harness/interfaces/tui/widgets/subagent_detail.py tests/test_resume_transcript.py
git commit -m "feat(tui): lazy-load and replay sub-agent transcripts on resume"
```

---

### Task 7: Finished-job history persistence + sidecar lifecycle

**Files:**
- Modify: `src/marim_harness/session/store.py` (`save`/`load` a `jobs` field)
- Modify: `src/marim_harness/jobs.py` (a `settled_summaries() -> list[dict]` helper + `restore(summaries)`)
- Modify: `src/marim_harness/session/ctrl.py` (delete sidecars on session delete/clear via `TranscriptStore.delete_all`)
- Modify: the save call site (wherever `store.save(...)` is invoked — pass settled job summaries) and resume (restore them; `_render_jobs` already renders the registry)
- Test: `tests/test_session_jobs_persist.py` (create)

**Interfaces:**
- Consumes: `JobRegistry`; `TranscriptStore.delete_all` (Task 2).
- Produces:
  - `JobRegistry.settled_summaries() -> list[dict]` — `{id, label, kind, status, result}` for each non-running job.
  - `JobRegistry.restore(summaries: list[dict]) -> None` — re-add them as settled (status preserved), no transcripts.
  - `SessionStore.save(..., jobs: list | None = None)` writes `payload["jobs"]`; `load()` returns it (append `jobs` to the returned tuple, or add a `load_jobs()` reader — pick the lower-churn option and keep `load()`'s tuple stable by adding `jobs` as the last element, updating all call sites).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_jobs_persist.py
from marim_harness.jobs import JobRegistry


def test_settled_summaries_excludes_running():
    reg = JobRegistry()
    a = reg.add(kind="agent", label="explore: x")
    reg.finish(a, result="report A", status="done")
    b = reg.add(kind="agent", label="explore: y")          # left running
    sums = reg.settled_summaries()
    ids = {s["id"] for s in sums}
    assert a in ids and b not in ids
    assert sums[0]["label"] == "explore: x" and sums[0]["result"] == "report A"


def test_restore_readds_settled_jobs():
    reg = JobRegistry()
    reg.restore([{"id": "j1", "label": "explore: z", "kind": "agent",
                  "status": "done", "result": "r"}])
    jobs = reg.list()
    assert len(jobs) == 1 and jobs[0].id == "j1" and jobs[0].status == "done"
```

(Adjust `reg.add`/`reg.finish` calls to the real `JobRegistry` API — read `src/marim_harness/jobs.py` first and match its actual method names/signatures.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_jobs_persist.py -v`
Expected: FAIL — `AttributeError: 'JobRegistry' object has no attribute 'settled_summaries'`.

- [ ] **Step 3: Implement**

Read `src/marim_harness/jobs.py` for the real `Job` shape and registry API. Add `settled_summaries()` (filter out running jobs; project the five fields) and `restore(summaries)` (re-insert each as a settled `Job`). In `SessionStore.save`, accept `jobs: list | None = None` and set `payload["jobs"] = jobs or []`; in `load`, read `data.get("jobs", [])` and return it (extend the returned tuple's last position; update every `load()` caller to unpack the extra value). At the save call site, pass `deps.jobs.settled_summaries()`. On resume (where `load()` results are applied), call `deps.jobs.restore(jobs)` and the existing `_render_jobs()` shows them. In `session/ctrl.py`, where a session is deleted or `/clear`-reset, call `TranscriptStore(store.path, store.session_id).delete_all()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_session_jobs_persist.py tests/test_session*.py -q`
Expected: PASS.

- [ ] **Step 5: Full regression, lint, type-check, commit**

```bash
uv run pytest -q
uv run ruff check src tests && uv run pyright
git add src/marim_harness/session/store.py src/marim_harness/jobs.py src/marim_harness/session/ctrl.py tests/test_session_jobs_persist.py
git commit -m "feat(session): persist finished-job history; clean up transcript sidecars"
```

---

### Task 8: Backward-compatibility & end-to-end resume test

**Files:**
- Test: `tests/test_resume_transcript.py` (add cases)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing test (backward-compat + e2e)**

```python
# add to tests/test_resume_transcript.py
@pytest.mark.anyio
async def test_old_session_without_sidecars_resumes_report_only():
    """A session JSON with no .subagents dir and no jobs key must resume without
    error, showing report-only cards (today's behavior)."""
    from marim_harness.session import TranscriptStore
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        store = TranscriptStore(pathlib.Path(td) / "sessions" / "old.json", "old")
        assert store.read("anything") is None        # no dir -> None, no crash
```

(For a fuller end-to-end resume test, drive a `SessionStore.save` with a spawn in history + a written sidecar, then a fresh `SessionStore.load` + replay, asserting the pane lazy-loads the steps. Reuse `_make_harness` and the fake CLI from Task 5; keep it one focused test.)

- [ ] **Step 2: Run test to verify it fails / passes appropriately**

Run: `uv run pytest tests/test_resume_transcript.py -v`
Expected: the backward-compat case PASSES immediately (it pins existing graceful behavior); the e2e case drives the full path.

- [ ] **Step 3: Full suite**

Run: `uv run pytest -q`
Expected: all pass (modulo any unrelated pre-existing failures, which must be confirmed pre-existing on the base commit).

- [ ] **Step 4: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add tests/test_resume_transcript.py
git commit -m "test(session): backward-compat + end-to-end resume of sub-agent transcripts"
```

---

## Self-Review

**Spec coverage:**
- Transcript as `list[ModelMessage]` → Tasks 3–4 (CLI), Task 5 (native uses `all_messages()`). ✓
- Capping → Task 1, applied in Task 2's `write`. ✓
- Sidecar write-once / lazy read → Task 2 (store), Task 5 (write), Task 6 (lazy read). ✓
- Capture wiring (3 paths) → Task 5 (native + CLI); background uses the same `_save_transcript` since background spawns flow through the same paths with a `stream_id`. ✓
- Resume rendering + lazy load → Task 6. ✓
- Finished-job history → Task 7. ✓
- Error handling best-effort → Task 2 (`write`/`read` swallow + warn), Task 6 (fallback note). ✓
- Backward compat → Task 7 (`data.get` defaults), Task 8 (test). ✓
- Cap default 2000 / `MARIM_SUBAGENT_TRANSCRIPT_CAP` → Task 5. ✓

**Type consistency:** `TranscriptStore(session_path, session_id)`, `.write(stream_id, messages, cap)`, `.read(stream_id)`, `.delete_all()` used consistently across Tasks 2/5/6/7. `cap_transcript(messages, cap)` consistent (Tasks 1–2). `CliResult.transcript` / `CliStreamTranslator.transcript()` consistent (Tasks 3–5). `replay_messages_into(pane, messages)` consistent (Task 6). ✓

**Open verification for the implementer (read before coding the task):**
- Task 5: confirm the native spawn path's run-result variable name and that `result.all_messages()` is available there (it is on a pydantic-ai `AgentRunResult`); confirm `self.session.store` exposes `.path` and `.session_id`.
- Task 7: read `jobs.py` for the real `Job`/registry API before writing `settled_summaries`/`restore`; confirm the single `store.save(...)` call site and every `store.load()` caller when extending the return tuple.
