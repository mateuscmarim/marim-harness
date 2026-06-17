# marim-harness Hooks Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generic, Claude-Code-compatible lifecycle-hook engine to marim-harness so external tools (agentmemory) integrate by configuration alone, running their existing CC hook scripts unmodified.

**Architecture:** A standalone `src/marim_harness/hooks/` subpackage (`events.py` constants, `config.py` loader, `runner.py` executor) mirroring the existing `mcp/` subpackage. A `Deps.hooks` field carries an optional `HookRunner`, built in `bootstrap`. The `Harness` fires events at existing seams (`run_turn`, the composed `event_stream_handler`, subagent runners, session start/end) and `SessionController.maybe_compact` fires `PreCompact`. Hooks run as subprocesses with the payload as JSON on stdin; injection events (`SessionStart`, `UserPromptSubmit`) parse `additionalContext` from stdout and prepend it to the turn prompt. No veto. Fail-soft everywhere.

**Tech Stack:** Python 3.11+, asyncio subprocesses, pydantic-ai (`FunctionModel`/`FunctionToolCallEvent`/`FunctionToolResultEvent`), pytest + anyio.

## Global Constraints

- **No veto/blocking** — hooks never deny a tool, block a prompt, or force continuation. CC `decision`/exit-2 fields are ignored.
- **Fail-soft everywhere** — a missing, failing, malformed, or timed-out hook is swallowed and never raises into a turn. Timeout = process-group SIGKILL (`start_new_session=True`, `os.killpg`), matching `tools/shell.py`.
- **Injection events** are exactly `SessionStart` and `UserPromptSubmit`; only their stdout is read for `additionalContext`. All other events are observe-only (return value ignored).
- **Config source** — global `~/.config/marim/hooks.json` always honored; project `.marim/hooks.json` honored only when `MARIM_TRUST_PROJECT_HOOKS` is truthy.
- **Contract is CC-identical** — top-level `hooks` key; per-event list of `{matcher?, hooks: [{type:"command", command, timeout?}]}`; `matcher` is a regex on `tool_name` for `PreToolUse`/`PostToolUse` only; default per-hook timeout is **30** seconds.
- **Payload** stdin JSON always includes `hook_event_name`, `session_id`, `cwd`, `transcript_path` (the latter two from the workspace root and the session store path; empty strings when storeless).
- **Secrets** — never write API keys to files or logs; the engine only ever passes the payload it builds.
- TDD: write the failing test, watch it fail, minimal code, watch it pass, commit. No mocks — use real subprocesses, real temp scripts, and `FunctionModel`.

---

### Task 1: Event constants (`events.py`)

**Files:**
- Create: `src/marim_harness/hooks/__init__.py`
- Create: `src/marim_harness/hooks/events.py`
- Test: `tests/test_hooks_events.py`

**Interfaces:**
- Produces: module `marim_harness.hooks.events` with string constants `SESSION_START`, `USER_PROMPT_SUBMIT`, `PRE_TOOL_USE`, `POST_TOOL_USE`, `PRE_COMPACT`, `SUBAGENT_START`, `SUBAGENT_STOP`, `STOP`, `SESSION_END`, and `INJECTING_EVENTS: frozenset[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hooks_events.py
from marim_harness.hooks import events


def test_event_constants_match_claude_code_names():
    assert events.SESSION_START == "SessionStart"
    assert events.USER_PROMPT_SUBMIT == "UserPromptSubmit"
    assert events.PRE_TOOL_USE == "PreToolUse"
    assert events.POST_TOOL_USE == "PostToolUse"
    assert events.PRE_COMPACT == "PreCompact"
    assert events.SUBAGENT_START == "SubagentStart"
    assert events.SUBAGENT_STOP == "SubagentStop"
    assert events.STOP == "Stop"
    assert events.SESSION_END == "SessionEnd"


def test_only_session_start_and_user_prompt_inject():
    assert events.INJECTING_EVENTS == frozenset(
        {events.SESSION_START, events.USER_PROMPT_SUBMIT}
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_hooks_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.hooks'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/marim_harness/hooks/__init__.py
"""Claude-Code-compatible lifecycle-hook engine. See
docs/superpowers/specs/2026-06-17-marim-hooks-engine-design.md."""

from .config import load_hooks_config
from .runner import HookRunner, base_payload

__all__ = ["load_hooks_config", "HookRunner", "base_payload"]
```

```python
# src/marim_harness/hooks/events.py
"""Hook event names (Claude Code's exact strings) and the set of events whose
stdout is read for injected context. Kept dependency-free (a leaf module) so it
can be imported anywhere without risking an import cycle."""

SESSION_START = "SessionStart"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
PRE_COMPACT = "PreCompact"
SUBAGENT_START = "SubagentStart"
SUBAGENT_STOP = "SubagentStop"
STOP = "Stop"
SESSION_END = "SessionEnd"

# Only these two events may inject context back into the turn (additionalContext).
INJECTING_EVENTS = frozenset({SESSION_START, USER_PROMPT_SUBMIT})
```

Note: `__init__.py` imports `config` and `runner`, created in Tasks 2 and 3. This test imports only `events`, so run it after those modules exist, OR temporarily import only `events` — but since the plan runs tasks in order, write `__init__.py` now and expect Task 1's test to pass once Tasks 2–3 land. To keep Task 1 self-contained, make `__init__.py` import-safe by deferring: replace its body with the two imports only after Task 3. For Task 1, write `__init__.py` as an empty docstring module:

```python
# src/marim_harness/hooks/__init__.py
"""Claude-Code-compatible lifecycle-hook engine. See
docs/superpowers/specs/2026-06-17-marim-hooks-engine-design.md."""
```

(The re-exports are added in Task 3, Step 3.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_hooks_events.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/hooks/__init__.py src/marim_harness/hooks/events.py tests/test_hooks_events.py
git commit -m "feat(hooks): event-name constants leaf module"
```

---

### Task 2: Config loader (`config.py`)

**Files:**
- Create: `src/marim_harness/hooks/config.py`
- Test: `tests/test_hooks_config.py`

**Interfaces:**
- Consumes: `marim_harness.config.config_dir` (existing — returns `~/.config/marim`, honors `XDG_CONFIG_HOME`).
- Produces:
  - `global_hooks_config_path() -> Path`
  - `project_hooks_config_path(workspace_root: Path) -> Path`
  - `load_hooks_config(workspace_root: Path, *, trust_project: bool) -> dict` — returns the merged `{event: [entry, ...]}` map (the contents of the top-level `hooks` key), global first then project (only if `trust_project`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hooks_config.py
import json
from pathlib import Path

import pytest

from marim_harness.hooks import config


def _write(path: Path, hooks: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path


def test_missing_files_yield_empty(xdg):
    assert config.load_hooks_config(xdg / "ws", trust_project=True) == {}


def test_loads_global(xdg):
    _write(config.global_hooks_config_path(), {"Stop": [{"hooks": []}]})
    cfg = config.load_hooks_config(xdg / "ws", trust_project=False)
    assert "Stop" in cfg


def test_project_ignored_without_trust(xdg):
    ws = xdg / "ws"
    _write(config.project_hooks_config_path(ws), {"Stop": [{"hooks": []}]})
    assert config.load_hooks_config(ws, trust_project=False) == {}


def test_project_loaded_with_trust(xdg):
    ws = xdg / "ws"
    _write(config.project_hooks_config_path(ws), {"Stop": [{"hooks": []}]})
    assert "Stop" in config.load_hooks_config(ws, trust_project=True)


def test_global_and_project_merge_per_event(xdg):
    ws = xdg / "ws"
    _write(config.global_hooks_config_path(), {"Stop": [{"hooks": [{"command": "g"}]}]})
    _write(config.project_hooks_config_path(ws), {"Stop": [{"hooks": [{"command": "p"}]}]})
    cfg = config.load_hooks_config(ws, trust_project=True)
    assert len(cfg["Stop"]) == 2  # both entries kept, concatenated


def test_malformed_file_is_skipped(xdg):
    p = config.global_hooks_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")
    assert config.load_hooks_config(xdg / "ws", trust_project=True) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_hooks_config.py -v`
Expected: FAIL with `AttributeError: module 'marim_harness.hooks.config' has no attribute ...` / `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/marim_harness/hooks/config.py
"""Load and merge hook definitions from a global and an optional (trusted)
project config. Mirrors ``mcp/config.py``. The on-disk shape is Claude Code's:
a top-level ``hooks`` object mapping an event name to a list of entries."""

import json
from pathlib import Path

from ..config import config_dir


def global_hooks_config_path() -> Path:
    """The global hooks config, a sibling of the global ``.env``/``mcp.json``."""
    return config_dir() / "hooks.json"


def project_hooks_config_path(workspace_root: Path) -> Path:
    """The project-local hooks config, under the workspace's ``.marim/``."""
    return Path(workspace_root) / ".marim" / "hooks.json"


def _read_hooks(path: Path) -> dict:
    """Read the ``hooks`` mapping from a config file. A missing or malformed file
    yields ``{}`` — a broken config is skipped, never fatal."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return hooks if isinstance(hooks, dict) else {}


def _merge_into(merged: dict, hooks: dict) -> None:
    for event, entries in hooks.items():
        if isinstance(entries, list):
            merged.setdefault(event, []).extend(entries)


def load_hooks_config(workspace_root: Path, *, trust_project: bool) -> dict:
    """Merge global hook entries with project entries (only when ``trust_project``)
    into one ``{event: [entry, ...]}`` map. Per-event lists are concatenated."""
    merged: dict = {}
    _merge_into(merged, _read_hooks(global_hooks_config_path()))
    if trust_project:
        _merge_into(merged, _read_hooks(project_hooks_config_path(workspace_root)))
    return merged
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_hooks_config.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/hooks/config.py tests/test_hooks_config.py
git commit -m "feat(hooks): global+project config loader with trust gating"
```

---

### Task 3: Hook runner (`runner.py`)

**Files:**
- Create: `src/marim_harness/hooks/runner.py`
- Modify: `src/marim_harness/hooks/__init__.py` (add the re-exports promised in Task 1)
- Test: `tests/test_hooks_runner.py`

**Interfaces:**
- Consumes: `marim_harness.hooks.events` constants.
- Produces:
  - `base_payload(event: str, *, session_id: str, cwd: str, transcript_path: str, **extra) -> dict`
  - `class HookRunner` with `__init__(self, config: dict)` and `async dispatch(self, event: str, payload: dict) -> Optional[str]`.
    - For injection events returns the newline-joined `additionalContext` across matching hooks (or `None`); for observe-only events always returns `None`.
    - Never raises.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hooks_runner.py
import os
import stat
from pathlib import Path

import pytest

from marim_harness.hooks import events
from marim_harness.hooks.runner import HookRunner, base_payload


def _script(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


def _entry(command: str, *, matcher: str | None = None, timeout: int | None = None) -> dict:
    hook = {"type": "command", "command": command}
    if timeout is not None:
        hook["timeout"] = timeout
    entry: dict = {"hooks": [hook]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def _payload(event: str, **extra) -> dict:
    return base_payload(event, session_id="s1", cwd="/tmp", transcript_path="/tmp/s.json", **extra)


@pytest.mark.anyio
async def test_payload_arrives_on_stdin(tmp_path):
    out = tmp_path / "seen.txt"
    cmd = _script(tmp_path, "h.sh", f"cat > {out}\n")
    runner = HookRunner({events.STOP: [_entry(cmd)]})
    await runner.dispatch(events.STOP, _payload(events.STOP))
    received = out.read_text()
    assert '"hook_event_name": "Stop"' in received
    assert '"session_id": "s1"' in received


@pytest.mark.anyio
async def test_injection_via_hook_specific_output(tmp_path):
    cmd = _script(
        tmp_path, "h.sh",
        'echo \'{"hookSpecificOutput": {"additionalContext": "RECALLED"}}\'\n',
    )
    runner = HookRunner({events.SESSION_START: [_entry(cmd)]})
    ctx = await runner.dispatch(events.SESSION_START, _payload(events.SESSION_START, source="startup"))
    assert ctx == "RECALLED"


@pytest.mark.anyio
async def test_injection_via_plain_stdout(tmp_path):
    cmd = _script(tmp_path, "h.sh", "echo PLAINTEXT\n")
    runner = HookRunner({events.USER_PROMPT_SUBMIT: [_entry(cmd)]})
    ctx = await runner.dispatch(events.USER_PROMPT_SUBMIT, _payload(events.USER_PROMPT_SUBMIT, prompt="hi"))
    assert ctx == "PLAINTEXT"


@pytest.mark.anyio
async def test_multiple_hooks_concatenate(tmp_path):
    a = _script(tmp_path, "a.sh", "echo AAA\n")
    b = _script(tmp_path, "b.sh", "echo BBB\n")
    runner = HookRunner({events.SESSION_START: [_entry(a), _entry(b)]})
    ctx = await runner.dispatch(events.SESSION_START, _payload(events.SESSION_START, source="startup"))
    assert ctx == "AAA\nBBB"


@pytest.mark.anyio
async def test_observe_event_returns_none_even_with_stdout(tmp_path):
    cmd = _script(tmp_path, "h.sh", "echo IGNORED\n")
    runner = HookRunner({events.POST_TOOL_USE: [_entry(cmd, matcher="*")]})
    ctx = await runner.dispatch(events.POST_TOOL_USE, _payload(events.POST_TOOL_USE, tool_name="bash"))
    assert ctx is None


@pytest.mark.anyio
async def test_matcher_filters_by_tool_name(tmp_path):
    out = tmp_path / "ran.txt"
    cmd = _script(tmp_path, "h.sh", f"echo ran >> {out}\n")
    runner = HookRunner({events.PRE_TOOL_USE: [_entry(cmd, matcher="edit_file")]})
    await runner.dispatch(events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="bash"))
    assert not out.exists()  # matcher 'edit_file' does not match tool 'bash'
    await runner.dispatch(events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="edit_file"))
    assert out.read_text().strip() == "ran"


@pytest.mark.anyio
async def test_nonzero_exit_yields_no_context(tmp_path):
    cmd = _script(tmp_path, "h.sh", "echo NOPE\nexit 1\n")
    runner = HookRunner({events.SESSION_START: [_entry(cmd)]})
    assert await runner.dispatch(events.SESSION_START, _payload(events.SESSION_START)) is None


@pytest.mark.anyio
async def test_missing_command_is_swallowed(tmp_path):
    runner = HookRunner({events.SESSION_START: [_entry("/no/such/binary/xyzzy")]})
    assert await runner.dispatch(events.SESSION_START, _payload(events.SESSION_START)) is None


@pytest.mark.anyio
async def test_timeout_is_killed_and_swallowed(tmp_path):
    cmd = _script(tmp_path, "h.sh", "sleep 5\necho LATE\n")
    runner = HookRunner({events.SESSION_START: [_entry(cmd, timeout=1)]})
    assert await runner.dispatch(events.SESSION_START, _payload(events.SESSION_START)) is None


@pytest.mark.anyio
async def test_unconfigured_event_returns_none():
    runner = HookRunner({})
    assert await runner.dispatch(events.STOP, _payload(events.STOP)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_hooks_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.hooks.runner'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/marim_harness/hooks/runner.py
"""Execute configured hooks as subprocesses: payload JSON on stdin, exit-0 stdout
read for injected context on injection events. Reuses the process-group SIGKILL
timeout discipline from ``tools/shell.py``. Never raises."""

import asyncio
import json
import os
import re
import signal
from typing import Optional

from .events import INJECTING_EVENTS, POST_TOOL_USE, PRE_TOOL_USE

_DEFAULT_TIMEOUT = 30


def base_payload(
    event: str, *, session_id: str, cwd: str, transcript_path: str, **extra
) -> dict:
    """Assemble a hook payload with the common Claude-Code fields plus any
    event-specific extras."""
    payload = {
        "hook_event_name": event,
        "session_id": session_id,
        "cwd": cwd,
        "transcript_path": transcript_path,
    }
    payload.update(extra)
    return payload


def _matches(matcher, event: str, tool_name: str) -> bool:
    """``matcher`` (a regex on the tool name) gates only the tool events; for all
    other events it is ignored. Absent/empty/``*`` matches everything."""
    if event not in (PRE_TOOL_USE, POST_TOOL_USE):
        return True
    if not matcher or matcher == "*":
        return True
    try:
        return re.search(matcher, tool_name) is not None
    except re.error:
        return False


def _extract_context(out: str) -> Optional[str]:
    """Pull ``additionalContext`` from a hook's stdout: either CC's structured
    JSON or, when not JSON, the plain text verbatim."""
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return out  # plain text
    if isinstance(data, dict):
        hso = data.get("hookSpecificOutput")
        if isinstance(hso, dict) and hso.get("additionalContext"):
            return str(hso["additionalContext"])
        if data.get("additionalContext"):
            return str(data["additionalContext"])
        return None  # valid JSON, but no context field
    return out


async def _run_one(command: str, payload: dict, timeout) -> Optional[str]:
    """Run one hook command, feeding ``payload`` as JSON on stdin. Returns stripped
    stdout on a clean exit-0 run, else ``None``. Swallows every failure."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None
    data = json.dumps(payload).encode()
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(input=data), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
        return None
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode(errors="replace").strip() or None


class HookRunner:
    """Holds the merged hook config and dispatches events to it."""

    def __init__(self, config: dict) -> None:
        self._config = config or {}

    async def dispatch(self, event: str, payload: dict) -> Optional[str]:
        """Run every hook configured for ``event`` whose matcher passes. Returns
        injected context for injection events, else ``None``. Never raises."""
        entries = self._config.get(event)
        if not entries:
            return None
        tool_name = str(payload.get("tool_name", ""))
        contexts: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not _matches(entry.get("matcher"), event, tool_name):
                continue
            for spec in entry.get("hooks", []) or []:
                if not isinstance(spec, dict) or spec.get("type") != "command":
                    continue
                command = spec.get("command")
                if not command:
                    continue
                timeout = spec.get("timeout", _DEFAULT_TIMEOUT)
                try:
                    out = await _run_one(str(command), payload, timeout)
                except Exception:
                    out = None  # belt-and-suspenders: a hook never breaks a turn
                if out and event in INJECTING_EVENTS:
                    ctx = _extract_context(out)
                    if ctx:
                        contexts.append(ctx)
        return "\n".join(contexts) if contexts else None
```

Then update `__init__.py` to the full re-exports:

```python
# src/marim_harness/hooks/__init__.py
"""Claude-Code-compatible lifecycle-hook engine. See
docs/superpowers/specs/2026-06-17-marim-hooks-engine-design.md."""

from .config import load_hooks_config
from .runner import HookRunner, base_payload

__all__ = ["load_hooks_config", "HookRunner", "base_payload"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_hooks_runner.py tests/test_hooks_events.py -v`
Expected: PASS (all). The events test now also exercises the populated `__init__`.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/hooks/runner.py src/marim_harness/hooks/__init__.py tests/test_hooks_runner.py
git commit -m "feat(hooks): subprocess runner with stdin payload and context injection"
```

---

### Task 4: Wire into Deps, config flag, and bootstrap

**Files:**
- Modify: `src/marim_harness/deps.py` (add the `hooks` field)
- Modify: `src/marim_harness/config/model.py` (add `trust_project_hooks` + env parse)
- Modify: `src/marim_harness/bootstrap.py:36` (build a `HookRunner`, pass into `Deps`)
- Test: `tests/test_bootstrap.py` (new test), `tests/test_config.py` (new test)

**Interfaces:**
- Consumes: `HookRunner`, `load_hooks_config` from Task 3/2; `ModelConfig` from existing config.
- Produces: `Deps.hooks: Optional[HookRunner]` (default `None`); `ModelConfig.trust_project_hooks: bool`; `build_harness` populates `deps.hooks`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_config.py
def test_trust_project_hooks_defaults_false(monkeypatch):
    from marim_harness.config.model import load_config
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    assert load_config().trust_project_hooks is False


def test_trust_project_hooks_env_truthy(monkeypatch):
    from marim_harness.config.model import load_config
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    assert load_config().trust_project_hooks is True
```

```python
# add to tests/test_bootstrap.py
def test_build_harness_sets_hooks_when_global_config_present(tmp_path, monkeypatch):
    import json
    from marim_harness.bootstrap import build_harness
    from marim_harness.permissions import Mode

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MARIM_API_KEY", "x")
    cfg_path = tmp_path / "xdg" / "marim" / "hooks.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": []}]}}))

    harness = build_harness(tmp_path / "ws", mode=Mode.ask)
    assert harness.deps.hooks is not None


def test_build_harness_hooks_none_without_config(tmp_path, monkeypatch):
    from marim_harness.bootstrap import build_harness
    from marim_harness.permissions import Mode

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MARIM_API_KEY", "x")

    harness = build_harness(tmp_path / "ws", mode=Mode.ask)
    assert harness.deps.hooks is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -k trust_project_hooks tests/test_bootstrap.py -k hooks -v`
Expected: FAIL — `ModelConfig` has no `trust_project_hooks`; `deps.hooks` is `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

In `src/marim_harness/deps.py`, add the import block and the field. After the existing imports add:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .hooks.runner import HookRunner
```

(Place the `TYPE_CHECKING` import next to the other `typing` import: change `from typing import Awaitable, Callable, Optional` to `from typing import TYPE_CHECKING, Awaitable, Callable, Optional`.)

Then add this field to the `Deps` dataclass, after `command_policy`:

```python
    # Optional Claude-Code-compatible hook engine. None when no hooks.json is
    # configured (every fire-point becomes a cheap ``is None`` no-op).
    hooks: Optional["HookRunner"] = None
```

In `src/marim_harness/config/model.py`:
- Add the field to `ModelConfig` (after `proactive_memory`):

```python
    # When true, project-local .marim/hooks.json hooks are honored; otherwise
    # only the global hooks config runs (supply-chain guard for cloned repos).
    trust_project_hooks: bool = False
```

- In `load_config`, after the `proactive_memory = _bool_env(...)` line add:

```python
    trust_project_hooks = _bool_env("MARIM_TRUST_PROJECT_HOOKS", False)
```

- Pass `trust_project_hooks=trust_project_hooks,` into all three `ModelConfig(...)` returns (local, google, openrouter), alongside the existing `proactive_memory=proactive_memory,`.

In `src/marim_harness/bootstrap.py`:
- Add to the imports near the top:

```python
from .hooks import HookRunner, load_hooks_config
```

- Replace the `deps = Deps(...)` construction (currently `bootstrap.py:36`) with:

```python
    hooks_cfg = load_hooks_config(workspace, trust_project=cfg.trust_project_hooks)
    hook_runner = HookRunner(hooks_cfg) if hooks_cfg else None
    deps = Deps(
        workspace_root=workspace,
        mode=mode,
        command_policy=command_policy,
        hooks=hook_runner,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py tests/test_bootstrap.py tests/test_deps.py -v`
Expected: PASS (existing tests still green; new ones pass). If `tests/test_deps.py` does not exist, omit it.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/deps.py src/marim_harness/config/model.py src/marim_harness/bootstrap.py tests/test_config.py tests/test_bootstrap.py
git commit -m "feat(hooks): wire HookRunner through Deps, config flag, and bootstrap"
```

---

### Task 5: Fire SessionStart + UserPromptSubmit with injection

**Files:**
- Modify: `src/marim_harness/agent.py` (Harness `__init__`, new `_hook_payload`/`session_start`/`session_end`, `run_turn` prepend + UserPromptSubmit dispatch)
- Test: `tests/test_agent.py` (new tests)

**Interfaces:**
- Consumes: `Deps.hooks` (Task 4), `base_payload` + event constants (Task 3/1).
- Produces on `Harness`:
  - `_hook_payload(self, event: str, **extra) -> dict`
  - `async session_start(self, source: str) -> None` — dispatches `SessionStart`, stashes returned context in `self._pending_hook_context`.
  - `async session_end(self, reason: str = "exit") -> None` — dispatches `SessionEnd`.
  - `run_turn` prepends `_pending_hook_context` (consumed once) and fires `UserPromptSubmit`, prepending its returned context.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_agent.py
import stat
import json as _json

from marim_harness.hooks.runner import HookRunner
from marim_harness.hooks import events as hook_events


def _hook_script(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


def _prompt_capturing_model(sink: list) -> FunctionModel:
    """Records the first user-prompt text it sees, then replies 'ok'."""
    def fn(messages, info):
        for msg in messages:
            for part in getattr(msg, "parts", []):
                content = getattr(part, "content", None)
                if isinstance(content, str) and part.__class__.__name__ == "UserPromptPart":
                    sink.append(content)
        return ModelResponse(parts=[TextPart(content="ok")])
    return FunctionModel(fn)


@pytest.mark.anyio
async def test_session_start_context_is_prepended_once(tmp_path):
    cmd = _hook_script(tmp_path, "ss.sh", "echo SESSION_CTX\n")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto,
                hooks=HookRunner({hook_events.SESSION_START: [{"hooks": [{"type": "command", "command": cmd}]}]}))
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    await harness.session_start("startup")
    await harness.run_turn("first")
    assert "SESSION_CTX" in sink[0]
    await harness.run_turn("second")
    assert "SESSION_CTX" not in sink[1]  # consumed; not repeated


@pytest.mark.anyio
async def test_user_prompt_submit_context_is_prepended(tmp_path):
    cmd = _hook_script(tmp_path, "ups.sh", "echo PROMPT_CTX\n")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto,
                hooks=HookRunner({hook_events.USER_PROMPT_SUBMIT: [{"hooks": [{"type": "command", "command": cmd}]}]}))
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    await harness.run_turn("do the thing")
    assert "PROMPT_CTX" in sink[0]
    assert "do the thing" in sink[0]


@pytest.mark.anyio
async def test_no_hooks_runs_turn_normally(tmp_path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)  # hooks=None
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    out = await harness.run_turn("hello")
    assert out == "ok"
    assert sink[0] == "hello"  # untouched
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent.py -k "session_start_context or user_prompt_submit_context or no_hooks_runs" -v`
Expected: FAIL — `Harness` has no `session_start`; prompt not modified.

- [ ] **Step 3: Write minimal implementation**

In `src/marim_harness/agent.py`, add to the imports (after the existing `from .deps import Deps`):

```python
from .hooks import events as hook_events
from .hooks.runner import base_payload
```

In `Harness.__init__`, after the `self._pending_error_note: Optional[str] = None` line, add:

```python
        # One-shot context returned by a SessionStart hook, prepended to the next
        # turn's prompt and consumed there (mirrors _pending_error_note).
        self._pending_hook_context: Optional[str] = None
```

Add these methods to `Harness` (e.g. just before `run_turn`):

```python
    def _hook_payload(self, event: str, **extra) -> dict:
        """Build a hook payload with the common fields drawn from the live
        session, plus any event-specific extras."""
        store = self.session.store
        return base_payload(
            event,
            session_id=store.session_id if store is not None else "",
            cwd=str(self.deps.workspace_root),
            transcript_path=str(store.path) if store is not None else "",
            **extra,
        )

    async def session_start(self, source: str) -> None:
        """Fire the SessionStart hook (``source`` is ``startup``/``resume``/
        ``clear``) and stash any returned context for the next turn's prompt."""
        if self.deps.hooks is None:
            return
        ctx = await self.deps.hooks.dispatch(
            hook_events.SESSION_START,
            self._hook_payload(hook_events.SESSION_START, source=source),
        )
        if ctx:
            self._pending_hook_context = ctx

    async def session_end(self, reason: str = "exit") -> None:
        """Fire the SessionEnd hook on teardown. Observe-only."""
        if self.deps.hooks is None:
            return
        await self.deps.hooks.dispatch(
            hook_events.SESSION_END,
            self._hook_payload(hook_events.SESSION_END, reason=reason),
        )
```

In `run_turn`, immediately after the `_pending_error_note` block (after the line `self._pending_error_note = None`), add:

```python
        # Prepend any SessionStart-injected context, once.
        if self._pending_hook_context:
            prompt = f"{self._pending_hook_context}\n\n{prompt}"
            self._pending_hook_context = None
        # Fire UserPromptSubmit and prepend any context it returns.
        if self.deps.hooks is not None:
            ctx = await self.deps.hooks.dispatch(
                hook_events.USER_PROMPT_SUBMIT,
                self._hook_payload(hook_events.USER_PROMPT_SUBMIT, prompt=prompt),
            )
            if ctx:
                prompt = f"{ctx}\n\n{prompt}"
```

(This sits just before the existing `user_prompt: Optional[str] = prompt` line.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_agent.py -v`
Expected: PASS (new tests pass; all existing agent tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/agent.py tests/test_agent.py
git commit -m "feat(hooks): fire SessionStart/UserPromptSubmit with context injection"
```

---

### Task 6: Fire PreToolUse + PostToolUse via the composed event handler

**Files:**
- Modify: `src/marim_harness/agent.py` (`run_turn` handler composition, new `_fire_tool_event`)
- Test: `tests/test_agent.py` (new test)

**Interfaces:**
- Consumes: `Deps.hooks`, event constants; pydantic-ai `FunctionToolCallEvent`/`FunctionToolResultEvent`.
- Produces on `Harness`: `async _fire_tool_event(self, event) -> None`; `run_turn` wraps `event_stream_handler` so each streamed event also fires the matching tool hook (observe-only).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_agent.py
@pytest.mark.anyio
async def test_pre_and_post_tool_use_fire(tmp_path):
    (tmp_path / "a.txt").write_text("foo")
    log = tmp_path / "toolhooks.log"
    cmd = _hook_script(
        tmp_path, "tool.sh",
        # read the payload, append "<event> <tool>" to the log
        f"python3 -c 'import sys,json; d=json.load(sys.stdin); "
        f"open({str(log)!r},\"a\").write(d[\"hook_event_name\"]+\" \"+d.get(\"tool_name\",\"\")+\"\\n\")'\n",
    )
    runner = HookRunner({
        hook_events.PRE_TOOL_USE: [{"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}],
        hook_events.POST_TOOL_USE: [{"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}],
    })
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, hooks=runner)
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.run_turn("change foo to bar")
    lines = log.read_text().splitlines()
    assert "PreToolUse edit_file" in lines
    assert any(line.startswith("PostToolUse edit_file") for line in lines)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent.py -k pre_and_post_tool_use -v`
Expected: FAIL — log file is never written (no tool hooks fired).

- [ ] **Step 3: Write minimal implementation**

In `src/marim_harness/agent.py`, add this method to `Harness`:

```python
    async def _fire_tool_event(self, event) -> None:
        """Map a streamed tool event to a Pre/PostToolUse hook (observe-only)."""
        from pydantic_ai.messages import (
            FunctionToolCallEvent,
            FunctionToolResultEvent,
        )

        if self.deps.hooks is None:
            return
        if isinstance(event, FunctionToolCallEvent):
            try:
                tool_input = event.part.args_as_dict()
            except Exception:
                tool_input = {}
            await self.deps.hooks.dispatch(
                hook_events.PRE_TOOL_USE,
                self._hook_payload(
                    hook_events.PRE_TOOL_USE,
                    tool_name=event.part.tool_name,
                    tool_input=tool_input,
                ),
            )
        elif isinstance(event, FunctionToolResultEvent):
            await self.deps.hooks.dispatch(
                hook_events.POST_TOOL_USE,
                self._hook_payload(
                    hook_events.POST_TOOL_USE,
                    tool_name=getattr(event.part, "tool_name", ""),
                    tool_response=str(getattr(event.part, "content", "")),
                ),
            )
```

In `run_turn`, compose the handler. After the `toolsets = self.mcp.live_toolsets()` line and before `while True:`, add:

```python
        # When hooks are configured, intercept each streamed tool event to fire
        # Pre/PostToolUse, then forward to the original handler (or drain if none).
        if self.deps.hooks is not None:
            _base_handler = event_stream_handler

            async def _hooked_handler(ctx, events):
                async def _relay():
                    async for event in events:
                        await self._fire_tool_event(event)
                        yield event

                if _base_handler is not None:
                    await _base_handler(ctx, _relay())
                else:
                    async for _ in _relay():
                        pass

            event_stream_handler = _hooked_handler
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_agent.py -v`
Expected: PASS (new test passes; existing agent tests green).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/agent.py tests/test_agent.py
git commit -m "feat(hooks): fire Pre/PostToolUse via composed event handler"
```

---

### Task 7: Fire PreCompact on compaction

**Files:**
- Modify: `src/marim_harness/session/ctrl.py` (`maybe_compact`)
- Test: `tests/test_session.py` (new test)

**Interfaces:**
- Consumes: `self.deps.hooks`, `base_payload`, event constants.
- Produces: `SessionController.maybe_compact` dispatches `PreCompact` (trigger `auto`) when a compaction actually runs.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_session.py
import stat as _stat

import pytest

from marim_harness.deps import Deps
from marim_harness.hooks.runner import HookRunner
from marim_harness.hooks import events as hook_events
from marim_harness.session.ctrl import SessionController


def _hook_cmd(tmp_path, log):
    p = tmp_path / "pc.sh"
    p.write_text(f"#!/usr/bin/env bash\ncat >> {log}\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | _stat.S_IEXEC | _stat.S_IRWXU)
    return str(p)


@pytest.mark.anyio
async def test_pre_compact_fires_when_compaction_runs(tmp_path):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    log = tmp_path / "pc.log"
    cmd = _hook_cmd(tmp_path, log)
    deps = Deps(
        workspace_root=tmp_path,
        hooks=HookRunner({hook_events.PRE_COMPACT: [{"hooks": [{"type": "command", "command": cmd}]}]}),
    )
    # A tiny token budget forces compaction of a non-trivial history.
    ctrl = SessionController(None, None, deps, max_context_tokens=1, keep_last_messages=1)
    ctrl.history = [
        ModelRequest(parts=[UserPromptPart(content="x" * 5000)]),
        ModelRequest(parts=[UserPromptPart(content="y" * 5000)]),
        ModelRequest(parts=[UserPromptPart(content="z" * 5000)]),
    ]
    await ctrl.maybe_compact()
    assert log.exists()
    assert '"hook_event_name": "PreCompact"' in log.read_text()


@pytest.mark.anyio
async def test_pre_compact_does_not_fire_without_compaction(tmp_path):
    log = tmp_path / "pc.log"
    cmd = _hook_cmd(tmp_path, log)
    deps = Deps(
        workspace_root=tmp_path,
        hooks=HookRunner({hook_events.PRE_COMPACT: [{"hooks": [{"type": "command", "command": cmd}]}]}),
    )
    ctrl = SessionController(None, None, deps, max_context_tokens=100_000, keep_last_messages=20)
    ctrl.history = []  # nothing to compact
    await ctrl.maybe_compact()
    assert not log.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session.py -k pre_compact -v`
Expected: FAIL — no log written (PreCompact not fired).

- [ ] **Step 3: Write minimal implementation**

In `src/marim_harness/session/ctrl.py`, add the imports at the top (after `from ..deps import Deps`):

```python
from ..hooks import events as hook_events
from ..hooks.runner import base_payload
```

In `maybe_compact`, replace the `if did:` block with:

```python
        if did:
            self.history = new_history
            if self.deps.hooks is not None:
                await self.deps.hooks.dispatch(
                    hook_events.PRE_COMPACT,
                    base_payload(
                        hook_events.PRE_COMPACT,
                        session_id=self.store.session_id if self.store is not None else "",
                        cwd=str(self.deps.workspace_root),
                        transcript_path=str(self.store.path) if self.store is not None else "",
                        trigger="auto",
                        custom_instructions="",
                    ),
                )
            if self.on_compact is not None:
                self.on_compact(before, len(self.history))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_session.py -v`
Expected: PASS (new tests pass; existing session tests green).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/session/ctrl.py tests/test_session.py
git commit -m "feat(hooks): fire PreCompact when a compaction runs"
```

---

### Task 8: Fire SubagentStart + SubagentStop

**Files:**
- Modify: `src/marim_harness/agent.py` (`_run_subagent`, `_run_background_subagent`)
- Test: `tests/test_agent.py` (new test)

**Interfaces:**
- Consumes: `Deps.hooks`, `_hook_payload`, event constants.
- Produces: both subagent runners dispatch `SubagentStart` (before `sub.run`) and `SubagentStop` (after, with `result`).

- [ ] **Step 1: Write the failing test**

This test drives `_run_subagent` directly. It needs a sub-agent definition discoverable under the workspace; create one, then point a hook at a log.

```python
# add to tests/test_agent.py
def _make_subagent_def(ws: Path, name: str = "helper") -> None:
    d = ws / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: A helper.\ntools: [read_file]\n---\n\nHelp out.\n",
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_subagent_start_and_stop_fire(tmp_path):
    _make_subagent_def(tmp_path)
    log = tmp_path / "sub.log"
    cmd = _hook_script(
        tmp_path, "sub.sh",
        f"python3 -c 'import sys,json; d=json.load(sys.stdin); "
        f"open({str(log)!r},\"a\").write(d[\"hook_event_name\"]+\"\\n\")'\n",
    )
    runner = HookRunner({
        hook_events.SUBAGENT_START: [{"hooks": [{"type": "command", "command": cmd}]}],
        hook_events.SUBAGENT_STOP: [{"hooks": [{"type": "command", "command": cmd}]}],
    })
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, hooks=runner)
    # A model the sub-agent will run: just reply 'sub-done'.
    harness = _make_harness(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="sub-done")])), deps)
    out = await harness._run_subagent("helper", "do a thing", "stream-1")
    assert "sub-done" in out
    lines = log.read_text().splitlines()
    assert "SubagentStart" in lines
    assert "SubagentStop" in lines
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent.py -k subagent_start_and_stop -v`
Expected: FAIL — log not written (events not fired).

- [ ] **Step 3: Write minimal implementation**

In `_run_subagent`, wrap the `sub.run(...)` call. Replace:

```python
        granted, unknown = self.mcp.granted_servers(mcp_names)
        result = await sub.run(
            task, deps=self.deps, toolsets=granted,
            event_stream_handler=self._subagent_handler(stream_id),
        )
```

with:

```python
        granted, unknown = self.mcp.granted_servers(mcp_names)
        if self.deps.hooks is not None:
            await self.deps.hooks.dispatch(
                hook_events.SUBAGENT_START,
                self._hook_payload(hook_events.SUBAGENT_START, subagent_type=type, task=task),
            )
        result = await sub.run(
            task, deps=self.deps, toolsets=granted,
            event_stream_handler=self._subagent_handler(stream_id),
        )
        if self.deps.hooks is not None:
            await self.deps.hooks.dispatch(
                hook_events.SUBAGENT_STOP,
                self._hook_payload(
                    hook_events.SUBAGENT_STOP, subagent_type=type, task=task,
                    result=result.output,
                ),
            )
```

In `_run_background_subagent`, similarly wrap its `result = await sub.run(task, deps=self.deps, toolsets=granted)`:

```python
        granted, unknown = self.mcp.granted_servers(mcp_names)
        if self.deps.hooks is not None:
            await self.deps.hooks.dispatch(
                hook_events.SUBAGENT_START,
                self._hook_payload(hook_events.SUBAGENT_START, subagent_type=type, task=task),
            )
        result = await sub.run(task, deps=self.deps, toolsets=granted)
        if self.deps.hooks is not None:
            await self.deps.hooks.dispatch(
                hook_events.SUBAGENT_STOP,
                self._hook_payload(
                    hook_events.SUBAGENT_STOP, subagent_type=type, task=task,
                    result=result.output,
                ),
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_agent.py -v`
Expected: PASS (new test passes; existing tests green).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/agent.py tests/test_agent.py
git commit -m "feat(hooks): fire SubagentStart/SubagentStop around spawns"
```

---

### Task 9: Fire Stop, and wire SessionStart/SessionEnd into the entry points

**Files:**
- Modify: `src/marim_harness/agent.py` (`run_turn` Stop dispatch)
- Modify: `src/marim_harness/interfaces/cli/headless.py` (session_start before run, session_end in finally)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`on_mount`, `on_unmount`, `start_new_session`, `reset_conversation`)
- Test: `tests/test_agent.py` (Stop test); `tests/test_headless.py` (session_start/session_end test, if the file exists — otherwise add to `tests/test_agent.py` driving the harness directly)

**Interfaces:**
- Consumes: `Harness.session_start`/`session_end` (Task 5), `Deps.hooks`.
- Produces: `run_turn` dispatches `Stop` after producing its final output; the TUI and headless entry points call `session_start`/`session_end`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_agent.py
@pytest.mark.anyio
async def test_stop_fires_at_turn_end(tmp_path):
    log = tmp_path / "stop.log"
    cmd = _hook_script(tmp_path, "stop.sh", f"cat >> {log}\n")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto,
                hooks=HookRunner({hook_events.STOP: [{"hooks": [{"type": "command", "command": cmd}]}]}))
    harness = _make_harness(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="done")])), deps)
    out = await harness.run_turn("anything")
    assert out == "done"
    assert '"hook_event_name": "Stop"' in log.read_text()


@pytest.mark.anyio
async def test_session_end_fires(tmp_path):
    log = tmp_path / "end.log"
    cmd = _hook_script(tmp_path, "end.sh", f"cat >> {log}\n")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto,
                hooks=HookRunner({hook_events.SESSION_END: [{"hooks": [{"type": "command", "command": cmd}]}]}))
    harness = _make_harness(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])), deps)
    await harness.session_end("exit")
    assert '"hook_event_name": "SessionEnd"' in log.read_text()
    assert '"reason": "exit"' in log.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent.py -k "stop_fires_at_turn_end or session_end_fires" -v`
Expected: FAIL — `test_stop_fires_at_turn_end` fails (no Stop dispatch). `test_session_end_fires` passes already if Task 5 landed `session_end`; if so, keep it as a regression guard.

- [ ] **Step 3: Write minimal implementation**

In `src/marim_harness/agent.py` `run_turn`, replace the tail:

```python
            self.session.persist()
            output = result.output
            await self._maybe_autoname()
            return output
```

with:

```python
            self.session.persist()
            output = result.output
            if self.deps.hooks is not None:
                await self.deps.hooks.dispatch(
                    hook_events.STOP, self._hook_payload(hook_events.STOP)
                )
            await self._maybe_autoname()
            return output
```

In `src/marim_harness/interfaces/cli/headless.py` `run_headless`, change the `try`/`finally`:

```python
    try:
        await harness.connect()  # open any configured MCP servers for this run
        await harness.session_start("resume" if harness.session.history else "startup")
        output = await harness.run_turn(prompt, event_stream_handler=handler)
    except Exception as exc:  # keep the failure surface small and scriptable
        print(f"{type(exc).__name__}: {exc}", file=err)
        return 1
    finally:
        await harness.session_end("exit")
        await harness.aclose()
```

In `src/marim_harness/interfaces/tui/app.py`:

- In `on_mount`, after `await self._connect_mcp(log)` (the last line), add:

```python
        await self.harness.session_start(
            "resume" if self.harness.session.history else "startup"
        )
```

- In `on_unmount`, before `await self.harness.aclose()`, add:

```python
        await self.harness.session_end("exit")
```

- In `start_new_session`, after `self.harness.new_session(name)`, add:

```python
        await self.harness.session_start("startup")
```

- In `reset_conversation`, after the reset call (`await self.harness....`/`self.harness.reset()` — whichever the method body uses), add:

```python
        await self.harness.session_start("clear")
```

(Read the current `reset_conversation` body at `app.py:329` and place the `session_start("clear")` call after the existing reset is performed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_agent.py tests/test_app.py -v` (and `tests/test_headless.py` if present)
Expected: PASS (new tests pass; existing TUI/headless tests green).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/agent.py src/marim_harness/interfaces/cli/headless.py src/marim_harness/interfaces/tui/app.py tests/test_agent.py
git commit -m "feat(hooks): fire Stop at turn end and wire SessionStart/SessionEnd into entry points"
```

---

### Task 10: agentmemory example bridge + docs + full-suite gate

**Files:**
- Create: `examples/agentmemory/hooks.json`
- Create: `examples/agentmemory/mcp.json`
- Create: `examples/agentmemory/README.md`
- Modify: `README.md` (document the hooks engine + `MARIM_TRUST_PROJECT_HOOKS` beside the existing MCP-trust notes)

**Interfaces:**
- Consumes: the whole engine (Tasks 1–9). No code, no new tests; this task is config + docs and a final full-suite run.

- [ ] **Step 1: Verify agentmemory's actual hook script names and port**

Run (best effort; the package may not be installed):

```bash
npm view @agentmemory/mcp version 2>/dev/null || echo "mcp package not found"
ls "$(npm root -g)/@agentmemory/agentmemory/plugin" 2>/dev/null || echo "plugin scripts not found locally"
```

Expected: either a version + a `plugin/` listing, or the "not found" fallbacks. If the real script names/port differ from the placeholders below, use the real ones and note the source in `examples/agentmemory/README.md`. If unavailable, keep the documented defaults (`@agentmemory/mcp`, port `3111`, `plugin/*.sh`) and add a one-line "verify against your install" note.

- [ ] **Step 2: Write the example config files**

```json
// examples/agentmemory/hooks.json  — copy to ~/.config/marim/hooks.json
// Replace $AM with agentmemory's install path (e.g. the output of
// `npm root -g`/@agentmemory/agentmemory). Verify script names against your
// install; see README.md in this directory.
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "$AM/plugin/session-start.sh" }] }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "$AM/plugin/observe.sh", "timeout": 10 }] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "$AM/plugin/observe.sh", "timeout": 10 }] }
    ]
  }
}
```

```json
// examples/agentmemory/mcp.json  — merge into ~/.config/marim/mcp.json
{
  "mcpServers": {
    "agentmemory": {
      "command": "npx",
      "args": ["-y", "@agentmemory/mcp"],
      "env": {
        "AGENTMEMORY_URL": "http://localhost:3111",
        "AGENTMEMORY_SECRET": "${AGENTMEMORY_SECRET}"
      }
    }
  }
}
```

- [ ] **Step 3: Write the example README**

````markdown
<!-- examples/agentmemory/README.md -->
# agentmemory integration

marim's hook engine speaks Claude Code's hook contract, so agentmemory's bundled
hook scripts run unmodified. Two layers:

1. **MCP (`memory_*` tools)** — merge `mcp.json` into `~/.config/marim/mcp.json`.
   Gives the model agentmemory's tools on demand. Keep this in the **global**
   config (a project `.marim/mcp.json` from a cloned repo is a supply-chain risk).
2. **Hooks (auto-capture + recall)** — copy `hooks.json` to
   `~/.config/marim/hooks.json`. `SessionStart` injects recalled context;
   `UserPromptSubmit`/`PostToolUse` observe the turn.

## Setup

1. Run the agentmemory server (default `http://localhost:3111`) and export
   `AGENTMEMORY_SECRET` in your environment (never commit it).
2. In `hooks.json`, replace `$AM` with agentmemory's install path.
3. Start marim. `/mcp` shows the `agentmemory` server; hooks fire automatically.

## Trust

Global hooks always run. Project-local `.marim/hooks.json` is ignored unless you
set `MARIM_TRUST_PROJECT_HOOKS=1` — only do that in repos you trust.

## Verify against your install

Script names (`session-start.sh`, `observe.sh`), the npm package
(`@agentmemory/mcp`), and the port (`3111`) reflect agentmemory's docs at the
time of writing. Confirm them against your installed version:
`ls "$(npm root -g)"/@agentmemory/agentmemory/plugin`.
````

- [ ] **Step 4: Document the engine in the top-level README**

Add a "Hooks" subsection near the existing MCP section of `README.md`. Read the MCP section first to match its tone, then insert:

```markdown
### Hooks

marim runs Claude-Code-compatible lifecycle hooks. Define them in
`~/.config/marim/hooks.json` (global, always honored) or `.marim/hooks.json`
(project — honored only when `MARIM_TRUST_PROJECT_HOOKS=1`, since project hooks
execute shell from the repo). Events: `SessionStart`, `UserPromptSubmit`,
`PreToolUse`, `PostToolUse`, `PreCompact`, `SubagentStart`, `SubagentStop`,
`Stop`, `SessionEnd`. The payload is JSON on stdin; `SessionStart` and
`UserPromptSubmit` may inject context via `additionalContext` on stdout. Hooks
never block a tool or a turn. See `examples/agentmemory/` for a worked setup.
```

- [ ] **Step 5: Run the full suite and commit**

Run: `pytest -q`
Expected: PASS — all tests green (the pre-existing count plus the new hook tests).

```bash
git add examples/agentmemory/ README.md
git commit -m "docs(hooks): agentmemory example bridge and README"
```

---

## Self-Review

**1. Spec coverage:**
- Contract mirror (events, stdin JSON, output protocol) → Tasks 1, 3, 5, 6. ✓
- Event scope capture+injection, no veto → all firing tasks observe-only except SessionStart/UserPromptSubmit injection (Task 5); no veto path anywhere. ✓
- Config source (global always, project gated) → Task 2 + `MARIM_TRUST_PROJECT_HOOKS` in Task 4. ✓
- Architecture (standalone subpackage, composed seams) → Tasks 1–3 (subpackage), 5–9 (seams). ✓
- `events.py`/`config.py`/`runner.py` interfaces → Tasks 1/2/3. ✓
- Payload schema (common + per-event fields) → `base_payload`/`_hook_payload` + per-event extras across Tasks 3, 5–9. ✓
- Output/injection protocol (JSON or plain stdout, concat, exit-0 only) → Task 3. ✓
- Wiring table (every event at its seam) → SessionStart/UserPromptSubmit (5), Pre/PostToolUse (6), PreCompact (7), Subagent (8), Stop/SessionEnd (9). ✓
- Deps.hooks + bootstrap gate → Task 4. ✓
- Error handling/security (fail-soft, timeout SIGKILL, trust) → Task 3 (runner), Task 2/4 (trust). ✓
- Testing (config, runner, agent integration, real subprocesses, no mocks) → Tasks 2, 3, 5–9. ✓
- agentmemory bridge (examples + docs) → Task 10. ✓

**2. Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N" — each step carries full code. The only deferred detail is agentmemory's exact script names (Task 10, Step 1), which is an explicit verification step against an external package, not a code placeholder.

**3. Type consistency:** `load_hooks_config(workspace_root, *, trust_project)`, `HookRunner(config)`, `HookRunner.dispatch(event, payload) -> Optional[str]`, `base_payload(event, *, session_id, cwd, transcript_path, **extra)`, `Harness._hook_payload(event, **extra)`, `Harness.session_start(source)`, `Harness.session_end(reason="exit")`, `Harness._fire_tool_event(event)`, `Deps.hooks`, `ModelConfig.trust_project_hooks`, `hook_events.*` — all used consistently across tasks. ✓
