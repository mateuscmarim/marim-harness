# Claude-CLI Sub-Agent Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `backend: claude-cli` spawns resumable: capture the CLI session id, checkpoint the parent sidecar mid-run, and resume interrupted CLI spawns by relaunching `claude -p --resume <session_id>` from the existing `r`-key surface.

**Architecture:** `ClaudeCliRunner.run` gains a `checkpoint` callback and captures the first `session_id` from the stream (the init event we already parse for the model). `_execute_cli_spawn` builds the sidecar meta template up front (with `backend: "claude-cli"` and `cli_session_id`) and threads the checkpoint closure through `_run_cli`. `resume_spawn` branches on `meta["backend"]`: the CLI branch skips transcript read/repair entirely and relaunches through the existing CLI tail with `resume_session_id` + `append_system=False` (the exact pattern the main-loop `ClaudeCliModel` already uses via `build_cli_argv`). Everything downstream — interrupted scan, ⏸ card, `r` key, `_resuming` guard, digest — is reused untouched.

**Tech Stack:** Python 3.10+, asyncio subprocess (fake-CLI test scripts), pydantic-ai message types, pytest + anyio.

**Spec:** `docs/superpowers/specs/2026-07-03-cli-subagent-resume-design.md`.

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`. Run that trio (in CI order) before claiming a task done.
- Python floor 3.10; ruff line length 100, lint set `E,F,I,UP,B,SIM`.
- Checkpoint failures log and continue — a checkpoint must never kill a spawn (`_save_transcript` already guarantees this; don't add a second try/except around it).
- CLI checkpoints pass `cap_reasoning=True` (safe: the translator's `ThinkingPart`s carry no provider signature).
- `append_system` must be False exactly when resuming (`append_system=resume_session_id is None`) — the session already carries its system prompt.
- Every resume refusal returns a user-renderable `(None, reason)` through the existing seam.
- Native spawns' meta is untouched: `backend` absent ⇒ native branch, no migration.
- No pre-flight check that the CLI session file exists — a stale session surfaces as the CLI's own error on the failed job.
- Demuxed children keep completion-time v1 sidecars (parent-only checkpoints — decided in the spec).
- Preserve the long "why" comments; write new ones in that style.

## Verified codebase facts the tasks rely on (at HEAD 3d44711)

- `build_cli_argv` (`src/marim_harness/subagents/cli_backend.py:146-187`) already accepts
  `resume_session_id` and `append_system` — the main-loop provider uses them. No argv
  changes needed, only threading.
- `ClaudeCliRunner.run` (cli_backend.py:428-521) parses each stream-json line, captures
  the model from the init event, routes demux traffic, and accumulates the transcript in
  `CliStreamTranslator._messages` (exposed via `transcript()`, a shallow copy). It does
  NOT capture `session_id`.
- `CliResult` (cli_backend.py:111-121) has `output/usage/transcript/child_transcripts`.
- `_run_cli` (`src/marim_harness/subagents/runner.py:1014-1050`) resolves
  binary/tools/model/cwd and calls `runner.run(...)`.
- `_execute_cli_spawn` (runner.py:939-999) wraps hooks/cap/worktree/persist and writes
  the final meta inline (runner.py:973-984). Its background failure path calls
  `self._discard_worktree(iso)` in both except arms (runner.py:958-971).
- `_save_transcript(self, stream_id, messages, meta=None, cap_reasoning=False)`
  (runner.py:492-499). `TranscriptStore.write` copies the meta dict before stamping
  `stream_id`/`updated`, so mutating a shared template between checkpoints is safe.
- `resume_spawn` (runner.py:1136-1209): `_resuming` sync guard in try/finally; guard
  order is store → `read_meta` → status → live-job scan → transcript read/repair →
  isolation → `_prepare_spawn` → register. `_CONTINUATION_PROMPT` at runner.py:1130.
- Native resume keeps the branch on a failed resumed run
  (`_execute_background_spawn`'s except arms switch on `history is not None`); the CLI
  tail has no such switch yet.
- `find_agent(root, type)` resolves an agent definition; `defn.backend` is
  `"claude-cli"` for CLI agents (see `_execute_spawn`, runner.py:657-658).
- Fake-CLI test pattern: `tests/test_subagent_transcript_capture.py` writes an
  executable python script, sets `MARIM_CLAUDE_CLI_BIN`, authors a `.marim/agents/*.md`
  agent with `backend: claude-cli`, builds a harness with `_make_harness(model, deps,
  store=SessionStore(...))`, and calls `harness.subagents.run(...)`.
- Locate existing argv/back-end unit tests with `grep -rln "build_cli_argv" tests/`
  before adding to them.

---

### Task 1: Session-id capture + checkpoint callback in the CLI backend

**Files:**
- Modify: `src/marim_harness/subagents/cli_backend.py`
- Test: the file `grep -rln "build_cli_argv\|CliStreamTranslator" tests/` points at
  (expected: `tests/test_cli_backend.py`; append there — if it doesn't exist, create it)

**Interfaces:**
- Consumes: nothing new.
- Produces (Task 2/3 rely on these exactly):
  `CliResult.session_id: str | None`;
  `ClaudeCliRunner.run(..., checkpoint: Callable[[list, str | None], None] | None = None,
  resume_session_id: str | None = None)` — `checkpoint(transcript_snapshot, session_id)`
  is called whenever the accumulated transcript has grown since the last call;
  `resume_session_id` threads into `build_cli_argv(resume_session_id=...,
  append_system=resume_session_id is None)`.

- [ ] **Step 1: Write the failing tests**

Append (adapting to the file's existing fake-process helpers if it has them; these are
self-contained otherwise):

```python
import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest

from marim_harness.subagents.cli_backend import ClaudeCliRunner

_FAKE_STREAM = '''#!{python}
import json, sys
for o in [
    {{"type": "system", "subtype": "init", "session_id": "sess-abc",
      "model": "claude-test"}},
    {{"type": "assistant", "message": {{"content": [
        {{"type": "text", "text": "step one"}}]}}}},
    {{"type": "assistant", "message": {{"content": [
        {{"type": "text", "text": "step two"}}]}}}},
    {{"type": "result", "subtype": "success", "result": "done", "num_turns": 1,
      "usage": {{"input_tokens": 1, "output_tokens": 1}}}},
]:
    sys.stdout.write(json.dumps(o) + "\\n")
'''


def _script(tmp_path: Path, body: str) -> str:
    p = tmp_path / "fake_claude.py"
    p.write_text(body.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


@pytest.mark.anyio
async def test_run_captures_session_id_and_checkpoints(tmp_path):
    seen: list[tuple[int, str | None]] = []

    def ckpt(messages: list, session_id: str | None) -> None:
        seen.append((len(messages), session_id))

    runner = ClaudeCliRunner(None, None)
    result = await runner.run(
        binary=_script(tmp_path, _FAKE_STREAM), prompt="task", system_prompt="sys",
        cwd=str(tmp_path), allow_gated=False, allowed_tools=[], model=None,
        stream_id="sg-cli", checkpoint=ckpt,
    )
    assert result.session_id == "sess-abc"
    # The transcript grew twice (two assistant messages); each growth checkpointed,
    # and every checkpoint after the init line carries the captured session id.
    assert [n for n, _ in seen] == [1, 2]
    assert all(sid == "sess-abc" for _, sid in seen)


@pytest.mark.anyio
async def test_resume_session_id_threads_into_argv(tmp_path):
    argv_file = tmp_path / "argv.json"
    body = (
        "#!{python}\n"
        "import json, sys\n"
        f"open({str(argv_file)!r}, 'w').write(json.dumps(sys.argv))\n"
        'sys.stdout.write(json.dumps({{"type": "result", "subtype": "success",'
        ' "result": "ok", "num_turns": 1, "usage": {{}}}}) + "\\n")\n'
    )
    runner = ClaudeCliRunner(None, None)
    await runner.run(
        binary=_script(tmp_path, body), prompt="continue", system_prompt="sys",
        cwd=str(tmp_path), allow_gated=False, allowed_tools=[], model=None,
        stream_id="sg-cli", resume_session_id="sess-abc",
    )
    argv = json.loads(argv_file.read_text())
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "sess-abc"
    assert "--append-system-prompt" not in argv  # session already has its prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov <located test file> -v`
Expected: FAIL — `run() got an unexpected keyword argument 'checkpoint'` (and
`'resume_session_id'`); `CliResult` has no `session_id`.

- [ ] **Step 3: Implement**

In `src/marim_harness/subagents/cli_backend.py`:

3a. Add `from collections.abc import Callable` to the imports.

3b. `CliResult` gains the field (after `child_transcripts`):

```python
    # The Claude session id captured from the stream's init event — the resume
    # key for `claude -p --resume`. None when the stream never reported one.
    session_id: str | None = None
```

3c. `ClaudeCliRunner.run` — extend the signature:

```python
    async def run(
        self, *, binary: str, prompt: str, system_prompt: str, cwd: str,
        allow_gated: bool, allowed_tools, model: str | None, stream_id: str,
        checkpoint: Callable[[list, str | None], None] | None = None,
        resume_session_id: str | None = None,
    ) -> CliResult:
```

and the argv build:

```python
        argv = build_cli_argv(
            binary, prompt, system_prompt,
            cli_permission_mode(allow_gated),
            map_tools_to_cc(allowed_tools), model,
            resume_session_id=resume_session_id,
            # A resumed session already carries its system prompt from creation;
            # re-appending would duplicate it (same rule as ClaudeCliModel's
            # resumed turns — see build_cli_argv's docstring).
            append_system=resume_session_id is None,
        )
```

3d. Inside the run loop: declare `session_id: str | None = None` and
`last_ckpt_len = 0` next to `results`/`model_sent`. At the TOP of the
`async for raw in _iter_ndjson_lines(...)` body — before the strip/parse — flush a
checkpoint for the *previous* line's growth:

```python
                # Checkpoint the transcript accumulated so far whenever it has
                # grown. Placed at the top of the iteration (not the bottom) so a
                # single call site covers every path the loop body takes — the
                # translate branch, the demux record_call/record_return path, and
                # all the `continue`s. The cost is a one-line lag: a kill mid-
                # stream loses at most the final line's content, and a clean run's
                # completion-time write supersedes the last checkpoint anyway.
                if checkpoint is not None:
                    snapshot = translator.transcript()
                    if len(snapshot) != last_ckpt_len:
                        last_ckpt_len = len(snapshot)
                        checkpoint(snapshot, session_id)
```

and right after `obj = json.loads(line)` succeeds, capture the id (before the demux
routing, so it's read off every raw line — first one wins):

```python
                if session_id is None:
                    sid = obj.get("session_id")
                    if isinstance(sid, str) and sid:
                        session_id = sid
```

After the loop (before building `CliResult`), flush the tail the top-of-loop check
lagged behind on:

```python
            if checkpoint is not None:
                snapshot = translator.transcript()
                if len(snapshot) != last_ckpt_len:
                    checkpoint(snapshot, session_id)
```

3e. Return it: `CliResult(..., session_id=session_id)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov <located test file> tests/test_subagent_transcript_capture.py -v`
Expected: PASS (new and neighbors).

- [ ] **Step 5: Gate and commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
git add src/marim_harness/subagents/cli_backend.py <located test file>
git commit -m "feat(subagents): capture CLI session id and checkpoint the stream"
```

---

### Task 2: CLI spawn meta + checkpoint threading in the runner

**Files:**
- Modify: `src/marim_harness/subagents/runner.py` (`_execute_cli_spawn`, `_run_cli`)
- Test: `tests/test_subagent_transcript_capture.py` (append)

**Interfaces:**
- Consumes: Task 1's `run(checkpoint=, resume_session_id=)` and `CliResult.session_id`.
- Produces (Task 3 relies on these exactly): CLI sidecar meta carrying
  `"backend": "claude-cli"` and `"cli_session_id": str | None` alongside the existing
  keys; `_run_cli(self, defn, task, work_root, model, stream_id, checkpoint=None,
  resume_session_id=None)`; `_execute_cli_spawn(..., background: bool,
  resume_session_id: str | None = None, original_task: str | None = None)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_transcript_capture.py` (reuse its `_fake_cli`/`_cli_agent`
helpers and `_session_store` shape; the fake must be extended to emit an init event —
update `_FAKE_CLI` to prepend `{"type": "system", "subtype": "init", "session_id":
"sess-abc", "model": "claude-test"}` to its object list, which is additive for the
existing test):

```python
@pytest.mark.anyio
async def test_cli_spawn_checkpoints_with_backend_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _cli_agent(tmp_path)
    store = SessionStore(path=tmp_path / "sessions" / "t.json", workspace_root=tmp_path,
                         session_id="t", name="t")
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])),
        _make_deps(tmp_path), store=store,
    )
    statuses: list[str | None] = []
    orig = harness.subagents._save_transcript

    def spy(stream_id, messages, meta=None, cap_reasoning=False):
        statuses.append(None if meta is None else meta.get("status"))
        orig(stream_id, messages, meta=meta, cap_reasoning=cap_reasoning)

    harness.subagents._save_transcript = spy
    await harness.subagents.run("cli-worker", "do it", stream_id="sg-cli")
    # Mid-run checkpoints say "running"; the parent's completion write is last
    # ("finished" — this fake spawns no Claude-side children, so no trailing
    # meta-less child write follows it).
    assert "running" in statuses and statuses[-1] == "finished"
    ts = TranscriptStore(store.path, store.session_id)
    meta = ts.read_meta("sg-cli")
    assert meta["backend"] == "claude-cli"
    assert meta["cli_session_id"] == "sess-abc"
    assert meta["status"] == "finished"


@pytest.mark.anyio
async def test_killed_cli_spawn_rests_at_running_with_session_id(tmp_path, monkeypatch):
    """A CLI process that dies without a result leaves the checkpointed sidecar
    at status=running with the session id — the resumable trail."""
    dead = tmp_path / "dead_claude.py"
    dead.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        'sys.stdout.write(json.dumps({"type": "system", "subtype": "init",'
        ' "session_id": "sess-dead", "model": "m"}) + "\\n")\n'
        'sys.stdout.write(json.dumps({"type": "assistant", "message": {"content":'
        ' [{"type": "text", "text": "partial"}]}}) + "\\n")\n'
        "sys.exit(1)\n"
    )
    dead.chmod(dead.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", str(dead))
    _cli_agent(tmp_path)
    store = SessionStore(path=tmp_path / "sessions" / "t.json", workspace_root=tmp_path,
                         session_id="t", name="t")
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])),
        _make_deps(tmp_path), store=store,
    )
    out = await harness.subagents.run("cli-worker", "do it", stream_id="sg-dead")
    assert "failed" in out  # foreground containment
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-dead")
    assert meta is not None
    assert meta["status"] == "running" and meta["cli_session_id"] == "sess-dead"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_transcript_capture.py -v`
Expected: the two new tests FAIL (`read_meta` lacks `backend`; killed spawn has no
sidecar at all). The pre-existing test must still pass.

- [ ] **Step 3: Implement**

In `src/marim_harness/subagents/runner.py`:

3a. `_execute_cli_spawn` — extend the signature's keyword tail:

```python
        model: str | None, stream_id: str, *, background: bool,
        resume_session_id: str | None = None, original_task: str | None = None,
```

3b. At the top of `_execute_cli_spawn`, before the hooks call, build the meta template
and checkpoint closure, and use the original task for hooks and meta (a resumed run's
`task` parameter is the internal continuation prompt — the meta must keep the ORIGINAL
task so the card, the jobs label on a later resume, and a resume-of-a-resume all read
the real ask; same rule as the native path's `prep.meta`):

```python
        hook_task = original_task or task
        meta: dict | None = None
        checkpoint = None
        if stream_id:
            # Same template the native path builds in _prepare_spawn, plus the two
            # CLI-only keys: `backend` routes resume_spawn to the CLI branch, and
            # `cli_session_id` (filled by the first checkpoint once the init event
            # arrives) is the `claude -p --resume` key. Mutating the shared
            # template between checkpoints is safe — TranscriptStore.write
            # snapshots the dict before stamping.
            meta = {
                "stream_id": stream_id, "type": defn.name, "task": hook_task,
                "model": model, "mcp": None, "depth": 1,
                "max_output_chars": max_output_chars,
                "isolation": iso["branch"] if iso else None,
                "status": "running",
                "backend": "claude-cli",
                "cli_session_id": resume_session_id,
            }

            def checkpoint(messages: list, session_id: str | None,
                           _meta=meta) -> None:
                if session_id:
                    _meta["cli_session_id"] = session_id
                self._save_transcript(stream_id, messages, meta=_meta,
                                      cap_reasoning=True)

        await self.hooks.subagent_start(defn.name, hook_task)
```

(the existing `await self.hooks.subagent_start(defn.name, task)` line is replaced by
the `hook_task` version above; the `subagent_stop` calls in the failure arm and the
success tail likewise switch `task` → `hook_task`.)

3c. Thread through the run call:

```python
                result = await self._run_cli(
                    defn, task, work_root, model, stream_id,
                    checkpoint=checkpoint, resume_session_id=resume_session_id,
                )
```

3d. Worktree preservation on a failed RESUMED run — in BOTH except arms, replace
`self._discard_worktree(iso)` with:

```python
            if iso:
                if resume_session_id is None:
                    self._discard_worktree(iso)
                else:
                    # A resumed spawn's branch holds prior committed work; a failed
                    # resume must not destroy it. Tear down only the worktree
                    # checkout and keep the branch (native-resume parity).
                    self._teardown_worktree(iso, force=True)
```

3e. Replace the inline final-meta dict (runner.py:973-984) with the template-based
write, folding in the captured session id:

```python
        final_meta = None
        if meta is not None:
            final_meta = {
                **meta,
                "status": "finished",
                "cli_session_id": result.session_id or meta["cli_session_id"],
                "usage": {"input": result.usage.input_tokens,
                          "output": result.usage.output_tokens},
            }
        self._save_transcript(stream_id, result.transcript, meta=final_meta)
```

3f. `_run_cli` — extend the signature and pass through:

```python
    async def _run_cli(self, defn, task: str, work_root, model: str | None,
                       stream_id: str, checkpoint=None,
                       resume_session_id: str | None = None) -> CliResult:
        ...
        result = await runner.run(
            binary=binary, prompt=task, system_prompt=defn.prompt, cwd=cwd,
            allow_gated=allow_gated, allowed_tools=tools, model=model_name,
            stream_id=stream_id, checkpoint=checkpoint,
            resume_session_id=resume_session_id,
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_subagent_transcript_capture.py tests/test_subagent_cli_spawn.py -v`
Expected: PASS.

- [ ] **Step 5: Gate and commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
git add src/marim_harness/subagents/runner.py tests/test_subagent_transcript_capture.py
git commit -m "feat(subagents): checkpoint CLI spawn sidecars with backend meta"
```

---

### Task 3: The CLI resume branch

**Files:**
- Modify: `src/marim_harness/subagents/runner.py` (`resume_spawn`, new `_resume_cli_spawn`)
- Modify: `CLAUDE.md` (one sentence)
- Test: `tests/test_subagent_resume.py` (append)

**Interfaces:**
- Consumes: Task 2's meta keys and `_execute_cli_spawn(..., resume_session_id=,
  original_task=)`; existing `find_agent`, `branch_exists`, `create_or_reuse_worktree`,
  `_CONTINUATION_PROMPT`, `_resuming` guard.
- Produces: `resume_spawn` transparently handles CLI spawns; no new public API.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_resume.py` (reuse its `_session_store` helper; the
fake-CLI/argv-capture helpers mirror Task 1's — import-free duplication in this file is
fine, the suites are independent):

```python
import stat


def _cli_meta(sid: str, session: str | None = "sess-abc") -> dict:
    return {"stream_id": sid, "type": "cli-worker", "task": "original cli task",
            "model": None, "mcp": None, "depth": 1, "max_output_chars": None,
            "isolation": None, "status": "running",
            "backend": "claude-cli", "cli_session_id": session}


def _cli_agent(tmp_path):
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cli-worker.md").write_text(
        "---\ndescription: w\nbackend: claude-cli\ntools: read_file\n---\nWork.\n"
    )


def _resume_fake_cli(tmp_path, argv_file):
    p = tmp_path / "fake_claude_resume.py"
    p.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(argv_file)!r}, 'w').write(json.dumps(sys.argv))\n"
        'sys.stdout.write(json.dumps({"type": "system", "subtype": "init",'
        ' "session_id": "sess-abc", "model": "m"}) + "\\n")\n'
        'sys.stdout.write(json.dumps({"type": "result", "subtype": "success",'
        ' "result": "resumed-cli-ok", "num_turns": 1,'
        ' "usage": {"input_tokens": 1, "output_tokens": 1}}) + "\\n")\n'
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


@pytest.mark.anyio
async def test_resume_cli_spawn_relaunches_with_resume_flag(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _resume_fake_cli(tmp_path, argv_file))
    _cli_agent(tmp_path)
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    ts.write("sg-cli", _dangling_history(), 2000, meta=_cli_meta("sg-cli"))
    job_id, message = await harness.subagents.resume_spawn("sg-cli")
    assert job_id is not None, message
    report = await harness.deps.jobs.wait(job_id)
    assert report == "resumed-cli-ok"
    import json as _json
    argv = _json.loads(argv_file.read_text())
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "sess-abc"
    assert "--append-system-prompt" not in argv
    assert argv[argv.index("-p") + 1].startswith("You were interrupted")
    meta = ts.read_meta("sg-cli")
    assert meta["status"] == "finished"
    assert meta["task"] == "original cli task"  # continuation prompt never leaks in


@pytest.mark.anyio
async def test_resume_cli_refusals(tmp_path, monkeypatch):
    _cli_agent(tmp_path)
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    # No session id recorded (killed before init) → refuse, don't run the CLI.
    ts.write("sg-nosid", _dangling_history(), 2000,
             meta=_cli_meta("sg-nosid", session=None))
    job_id, msg = await harness.subagents.resume_spawn("sg-nosid")
    assert job_id is None and "never recorded" in msg
    # Agent type vanished → refuse.
    ts.write("sg-gone", _dangling_history(), 2000,
             meta={**_cli_meta("sg-gone"), "type": "no-such-agent"})
    job_id, msg = await harness.subagents.resume_spawn("sg-gone")
    assert job_id is None and "no-such-agent" in msg
    # Backend changed out from under the sidecar → refuse.
    d = tmp_path / ".marim" / "agents"
    (d / "flipped.md").write_text("---\ndescription: w\ntools: read_file\n---\nWork.\n")
    ts.write("sg-flip", _dangling_history(), 2000,
             meta={**_cli_meta("sg-flip"), "type": "flipped"})
    job_id, msg = await harness.subagents.resume_spawn("sg-flip")
    assert job_id is None and "no longer claude-cli" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_resume.py -v`
Expected: the new tests FAIL — the CLI-backed sidecar goes down the NATIVE branch
(`_prepare_spawn`/`build` on a CLI defn behaves wrongly or the argv file never appears).

- [ ] **Step 3: Implement the branch**

In `src/marim_harness/subagents/runner.py`:

3a. In `resume_spawn`, insert the branch right after the live-job scan loop (before
`messages = store.read(stream_id)`), still inside the `_resuming` try/finally:

```python
            # A claude-cli spawn resumes through the CLI's own session machinery,
            # not the native transcript-repair path: the CLI owns its history and
            # marim's sidecar is a display copy, so reading/repairing it here
            # would be wasted work at best and engine-swapping at worst.
            if meta.get("backend") == "claude-cli":
                return await self._resume_cli_spawn(stream_id, meta)
```

3b. Add the method (place after `resume_spawn`):

```python
    async def _resume_cli_spawn(self, stream_id: str,
                                meta: dict) -> tuple[str | None, str]:
        """Resume an interrupted claude-cli spawn by relaunching the CLI with
        ``--resume`` on its recorded session id, as a background job. The caller
        (resume_spawn) already holds the ``_resuming`` guard and has verified the
        sidecar status and the absence of a live job. There is deliberately no
        pre-flight check that the CLI session file still exists — its on-disk
        scheme is CLI-internal, so a stale session surfaces as the CLI's own
        error on the failed job instead of a brittle path probe here."""
        session_id = meta.get("cli_session_id")
        if not session_id:
            return None, ("The CLI session id was never recorded (the spawn died "
                          "before its session started) — nothing to resume; "
                          "spawn it again instead.")
        type_ = str(meta.get("type") or "")
        task = str(meta.get("task") or "")
        defn = find_agent(self.deps.workspace.root, type_)
        if defn is None:
            return None, f"No sub-agent type {type_!r} anymore — can't resume."
        if defn.backend != "claude-cli":
            return None, (f"Sub-agent type {type_!r} is no longer claude-cli "
                          "backed — can't resume its CLI session.")
        iso = None
        branch = meta.get("isolation")
        if branch:
            repo = repo_root(self.deps.workspace.root)
            if repo is None or not branch_exists(repo, branch):
                return None, (f"Isolation branch {branch!r} no longer exists — "
                              "can't resume this isolated spawn.")
            try:
                path = create_or_reuse_worktree(repo, branch)
            except WorktreeError as exc:
                return None, f"Couldn't reopen the isolated worktree: {exc}"
            iso = {"repo": repo, "branch": branch, "path": path}
        label = f"{type_}: resumed — {task}"
        job_id = self.deps.jobs.register(
            "agent", label,
            self._execute_cli_spawn(
                defn, self._CONTINUATION_PROMPT,
                iso["path"] if iso else None, iso,
                None, meta.get("max_output_chars"), meta.get("model"), stream_id,
                background=True, resume_session_id=session_id,
                original_task=task,
            ),
            stream_id=stream_id,
        )
        return job_id, f"Resumed as {job_id}."
```

3c. `CLAUDE.md` — in the paragraph describing the `claude-cli` provider/backend, extend
the sub-agents sentence to note resume: after "rendered as first-class cards in the
sub-agents screen, for both the main-loop provider and `backend: claude-cli` spawns.",
append: "Interrupted `claude-cli` spawns resume via the CLI's own `--resume` (the
session id is checkpointed in the spawn's sidecar meta)."

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_subagent_resume.py tests/test_subagent_transcript_capture.py tests/test_subagent_cli_spawn.py -v`
Expected: PASS — including all pre-existing native-resume tests (the branch must not
disturb them).

- [ ] **Step 5: Gate and commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
git add src/marim_harness/subagents/runner.py tests/test_subagent_resume.py CLAUDE.md
git commit -m "feat(subagents): resume claude-cli spawns via the CLI's --resume"
```

---

### Task 4: Verification

**Files:** none — the full gate plus an opt-in live check.

- [ ] **Step 1: Full gate in CI order**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: all green.

- [ ] **Step 2: Live check — OPT-IN ONLY**

A real run uses the user's `claude` binary on their Claude subscription. Do NOT run it
without the user's explicit go-ahead. If approved: in a scratch workspace with a
`backend: claude-cli` agent, spawn it in the TUI (background), `kill -9` the marim
process mid-run, relaunch with `--resume`, confirm the ⏸ card, press `r`, and confirm
the CLI continues the same session (its output references the earlier context) and the
sidecar meta transitions `running → finished` with `cli_session_id` intact. Otherwise,
the fake-CLI e2e tests from Tasks 1–3 stand as the verification.
