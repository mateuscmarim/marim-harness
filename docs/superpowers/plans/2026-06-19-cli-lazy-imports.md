# Lazy CLI Imports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `marim config`, `marim models`, and `marim --help` start in ~0.1s instead of ~1.3s by importing the heavy agent machinery (`pydantic_ai`) only when a path that actually needs it runs.

**Architecture:** Two localized import-deferral changes. (1) `router.py` dispatches management subcommands by importing the matched module inside `main()` instead of importing all of them at module load. (2) `default_cmd.py` moves the four `pydantic_ai`-pulling imports (`build_harness`, `Mode`, `HarnessApp`, `run_headless`) into `run_default()` after argparse, so `--help`/arg-errors exit before they load. Subprocess "import-invariant" tests lock the win in.

**Tech Stack:** Python 3.10+, pytest, `subprocess` (fresh-interpreter import checks), ruff (line-length 100), pyright.

## Global Constraints

- **No behavior change** other than startup speed. Same CLI keywords, same dispatch contract (`module.main(argv[1:])` → `SystemExit(code)`), same TUI/headless launch.
- The TUI/headless launch and `marim sessions` still import `pydantic_ai` — **out of scope** (they need the agent / a shared core module). Do NOT touch `session.ctrl` or `permissions.py`.
- ruff line-length 100; pyright must stay green; the full `uv run pytest` suite must stay green.
- Tests live flat in `tests/`. Run with `uv run pytest`.

---

### Task 1: Lazy subcommand dispatch in `router.py`

Stop importing the subcommand modules at load time; import only the matched one inside `main()`. This alone makes `marim config`/`marim models` skip `pydantic_ai`.

**Files:**
- Modify: `src/marim_harness/interfaces/cli/router.py`
- Test: `tests/test_cli_startup.py` (create)

**Interfaces:**
- Produces: `marim_harness.interfaces.cli.router` no longer imports `config`/`models`/`sessions`/`default_cmd` at module load. `main()` keeps its signature (`() -> None`) and exit-code contract.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_startup.py`:

```python
import subprocess
import sys


def _imports_pydantic_ai(module: str) -> bool:
    """True if importing `module` in a FRESH interpreter pulls in pydantic_ai.

    Must be a subprocess: sys.modules is process-global, and the rest of the test
    suite imports pydantic_ai, so an in-process check would always see it loaded.
    """
    code = (
        f"import {module}\n"
        "import sys\n"
        "raise SystemExit(1 if 'pydantic_ai' in sys.modules else 0)"
    )
    return subprocess.run([sys.executable, "-c", code]).returncode == 1


def test_router_import_does_not_load_pydantic_ai():
    # Importing the CLI router must not drag in pydantic_ai, or every command
    # (config/models/--help) pays ~1s for an agent it never builds.
    assert not _imports_pydantic_ai("marim_harness.interfaces.cli.router")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli_startup.py::test_router_import_does_not_load_pydantic_ai -v`
Expected: FAIL (router currently imports `sessions` + `default_cmd`, which pull `pydantic_ai`).

- [ ] **Step 3: Make the router dispatch lazy**

In `src/marim_harness/interfaces/cli/router.py`, delete the four eager subcommand imports:

```python
from . import config as config_cmd
from . import models as models_cmd
from . import sessions as sessions_cmd
from .default_cmd import run_default
```

Replace the `_MANAGEMENT` dict with a set of keywords:

```python
# Reserved first-token keywords. argparse subparsers would claim the workspace
# positional, so we route manually before any parser sees the args.
_MANAGEMENT = {"sessions", "config", "models"}
```

And rewrite `main()` to import only the chosen path:

```python
def main() -> None:
    load_environment()
    _setup_logging()
    argv = sys.argv[1:]
    if argv and argv[0] in _MANAGEMENT:
        # Import only the chosen management command so the common, non-agent
        # commands (config/models) don't pay for pydantic_ai via their siblings.
        from importlib import import_module

        module = import_module(f".{argv[0]}", __package__)
        raise SystemExit(module.main(argv[1:]))
    from .default_cmd import run_default

    raise SystemExit(run_default(argv))
```

Leave the top-of-file `from ...config import load_environment`, `import logging/os/sys`, and `_setup_logging` untouched (they are light — `config.load_environment` does not import `pydantic_ai`).

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_cli_startup.py -v`
Expected: PASS.

- [ ] **Step 5: Verify dispatch still works + suite green**

```bash
uv run pytest tests/test_cli_startup.py -q
# Existing CLI dispatch tests must stay green (they call router.main / the
# config/models/sessions commands): test_cli.py, test_config_cli.py,
# test_models_cli.py, test_sessions_cli.py.
uv run pytest tests/test_cli.py tests/test_config_cli.py tests/test_models_cli.py tests/test_sessions_cli.py -q
uv run ruff check src/marim_harness/interfaces/cli/router.py tests/test_cli_startup.py
uv run pyright src/marim_harness/interfaces/cli/router.py
```
Expected: all pass; ruff clean; pyright 0 errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/cli/router.py tests/test_cli_startup.py
git commit -m "perf(cli): lazy subcommand dispatch so config/models skip pydantic_ai"
```

---

### Task 2: Defer heavy imports in `default_cmd.py`

Move the four `pydantic_ai`-pulling imports into `run_default()` after argparse, so `marim --help` and arg-validation errors are fast too. Add the matching invariant test plus a dispatch smoke test.

**Files:**
- Modify: `src/marim_harness/interfaces/cli/default_cmd.py`
- Test: `tests/test_cli_startup.py` (append)

**Interfaces:**
- Consumes: `marim_harness.interfaces.cli.router` (lazy dispatch from Task 1).
- Produces: `marim_harness.interfaces.cli.default_cmd` imports clean (no `pydantic_ai`); `run_default(argv, *, stdin, out, err) -> int` keeps its signature and behavior.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_startup.py`:

```python
def test_default_cmd_import_does_not_load_pydantic_ai():
    # The default command's module must stay import-clean so `marim --help` and
    # arg-validation errors exit before pydantic_ai loads. The real TUI/headless
    # launch still imports it inside run_default() — that's expected and untested
    # here.
    assert not _imports_pydantic_ai("marim_harness.interfaces.cli.default_cmd")


def test_help_exits_fast_without_pydantic_ai(tmp_path):
    # `marim --help` must print usage and exit 0 in a fresh interpreter without
    # ever importing pydantic_ai.
    code = (
        "import sys\n"
        "sys.argv = ['marim', '--help']\n"
        "from marim_harness.interfaces.cli.router import main\n"
        "try:\n"
        "    main()\n"
        "except SystemExit as e:\n"
        "    assert e.code in (0, None), e.code\n"
        "assert 'pydantic_ai' not in sys.modules, 'pydantic_ai loaded on --help'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli_startup.py -k "default_cmd or help_exits" -v`
Expected: both FAIL (default_cmd currently imports `build_harness`/`Mode`/`HarnessApp`/`run_headless` at module top, all of which pull `pydantic_ai`).

- [ ] **Step 3: Move the heavy imports into `run_default`**

In `src/marim_harness/interfaces/cli/default_cmd.py`, change the top-of-file imports from:

```python
import argparse
import asyncio
import sys
from pathlib import Path

from ...bootstrap import build_harness
from ...history import PromptHistory, default_history_path
from ...permissions import Mode
from ..tui.app import HarnessApp
from .headless import run_headless
```

to keep only the light ones at the top:

```python
import argparse
import asyncio
import sys
from pathlib import Path

from ...history import PromptHistory, default_history_path
```

Then add the deferred imports inside `run_default`, after the worktree handling and before the launch branches. The current body is:

```python
    if args.worktree:
        workspace = _enter_worktree(workspace, args.worktree, err)
        if workspace is None:
            return 2

    if _is_headless(args.prompt, stdin_isatty=stdin.isatty()):
        prompt = args.prompt if isinstance(args.prompt, str) else stdin.read()
        prompt = (prompt or "").strip()
        if not prompt:
            print("no prompt provided", file=err)
            return 2
        mode = Mode(args.mode) if args.mode else Mode.auto
        harness = build_harness(workspace, mode=mode, resume=args.resume)
        return asyncio.run(
            run_headless(harness, prompt, args.output_format, out=out, err=err)
        )

    harness = build_harness(workspace, mode=Mode.ask, resume=args.resume)
    HarnessApp(harness, history=PromptHistory(default_history_path())).run()
    return 0
```

Change it to import the heavy names locally at the point of launch:

```python
    if args.worktree:
        workspace = _enter_worktree(workspace, args.worktree, err)
        if workspace is None:
            return 2

    # Heavy imports (pydantic_ai) deferred to here so `--help` and arg errors stay
    # fast; only an actual launch pays for the agent.
    from ...bootstrap import build_harness
    from ...permissions import Mode

    if _is_headless(args.prompt, stdin_isatty=stdin.isatty()):
        prompt = args.prompt if isinstance(args.prompt, str) else stdin.read()
        prompt = (prompt or "").strip()
        if not prompt:
            print("no prompt provided", file=err)
            return 2
        from .headless import run_headless

        mode = Mode(args.mode) if args.mode else Mode.auto
        harness = build_harness(workspace, mode=mode, resume=args.resume)
        return asyncio.run(
            run_headless(harness, prompt, args.output_format, out=out, err=err)
        )

    from ..tui.app import HarnessApp

    harness = build_harness(workspace, mode=Mode.ask, resume=args.resume)
    HarnessApp(harness, history=PromptHistory(default_history_path())).run()
    return 0
```

Note: `build_harness` + `Mode` are imported once before the branch (both branches need them); `run_headless` and `HarnessApp` are imported inside their own branches. Confirm `_build_parser()` and any other module-level code do NOT reference `Mode`/`build_harness`/`HarnessApp`/`run_headless` (grep below) — they don't today, but verify after editing.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli_startup.py -v`
Expected: all four tests PASS.

- [ ] **Step 5: Verify no stragglers + behavior unchanged + suite green**

```bash
# No module-level reference to a now-local name (must return nothing outside run_default):
grep -nE "\b(Mode|build_harness|HarnessApp|run_headless)\b" src/marim_harness/interfaces/cli/default_cmd.py
# Headless still works end-to-end (the real launch path that DOES use the heavy imports):
uv run pytest -k "headless or default_cmd or worktree" -q
uv run ruff check src/marim_harness/interfaces/cli/default_cmd.py tests/test_cli_startup.py
uv run pyright src/marim_harness/interfaces/cli/default_cmd.py
uv run pytest    # full suite green
```
Expected: the grep shows `Mode`/`build_harness`/`run_headless`/`HarnessApp` only inside `run_default` (no top-level use); headless tests pass; ruff clean; pyright 0 errors; full suite green.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/cli/default_cmd.py tests/test_cli_startup.py
git commit -m "perf(cli): defer agent imports in default_cmd so --help stays fast"
```

---

## Final verification

- [ ] **Confirm the win end-to-end**

```bash
# config/models import-clean and fast (fresh interpreter):
python - <<'PY'
import subprocess, sys, time
for m in ("config", "models"):
    t = time.perf_counter()
    r = subprocess.run([sys.executable, "-c",
        f"import marim_harness.interfaces.cli.{m}, sys;"
        " raise SystemExit('pydantic_ai' in sys.modules)"])
    print(f"{m}: {(time.perf_counter()-t)*1000:.0f}ms  pydantic_ai_loaded={bool(r.returncode)}")
PY
uv run pytest -q          # whole suite green
uv run ruff check src tests
uv run pyright
```
Expected: `config`/`models` report `pydantic_ai_loaded=False` and well under 300ms; suite green; ruff + pyright clean.
