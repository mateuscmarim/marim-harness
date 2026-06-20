# Lazy CLI imports — faster startup for non-agent commands — design

**Date:** 2026-06-19
**Status:** Approved (design); implementation plan to follow.

## Goal

`marim config`, `marim models`, `marim --help`, and argument-error paths take ~1.3s
to start because `cli/router.py` eagerly imports every subcommand module at load
time, and two of those (`sessions`, `default_cmd`) transitively import the whole
`pydantic_ai` package (~1.0s). These commands never build an agent, so they should
not pay for one. **Make them import only what the chosen path needs, dropping their
startup from ~1.3s to ~0.1s.** No behavior change otherwise.

## Profiling (measured)

- `import marim_harness.interfaces.cli.router` → **~1.3s**, of which **~1.0s is
  `pydantic_ai`** (pulled via `router` → `sessions` → `session.ctrl` →
  `from pydantic_ai.usage import RunUsage`, which triggers the entire `pydantic_ai`
  package: agent, capabilities, mcp, pydantic_graph).
- Importing `cli.config` alone → **90ms** (`pydantic_ai` NOT loaded).
- Importing `cli.models` alone → **123ms** (`pydantic_ai` NOT loaded).

So the only reason `config`/`models` are slow is the router's eager sibling imports.

## Scope

**In scope:** `marim config`, `marim models`, `marim --help`, and arg-validation
error exits become ~0.1s.

**Out of scope (and why):**
- **The TUI / headless launch** (`marim`, `marim "<prompt>"`) still imports
  `pydantic_ai` — it builds the agent, so the cost is unavoidable. Painting the UI
  before the agent is built is a separate, larger effort, deliberately not done here.
- **`marim sessions`** still pays ~1s: `session.ctrl` imports `pydantic_ai.usage`
  at module level (`RunUsage`), and that module is shared with the TUI. Breaking it
  is higher-risk than the two localized import moves below; left as a possible
  follow-up, not part of this work.

## Changes (two files)

### 1. `src/marim_harness/interfaces/cli/router.py` — lazy subcommand dispatch

Today `main()` relies on four eager module-level imports and a dict of callables:

```python
from . import config as config_cmd
from . import models as models_cmd
from . import sessions as sessions_cmd
from .default_cmd import run_default

_MANAGEMENT = {
    "sessions": sessions_cmd.main,
    "config": config_cmd.main,
    "models": models_cmd.main,
}

def main() -> None:
    load_environment()
    _setup_logging()
    argv = sys.argv[1:]
    if argv and argv[0] in _MANAGEMENT:
        raise SystemExit(_MANAGEMENT[argv[0]](argv[1:]))
    raise SystemExit(run_default(argv))
```

Replace with a keyword **set** and import the matched module inside `main()`:

```python
_MANAGEMENT = {"sessions", "config", "models"}  # reserved first-token keywords

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

Behavior is identical (same keywords, same `module.main(argv[1:])` call, same
`SystemExit(code)`), but `marim config` now imports only `cli.config`.

### 2. `src/marim_harness/interfaces/cli/default_cmd.py` — defer heavy imports

Today the module top imports the agent-building machinery **and `Mode`**:

```python
from ...bootstrap import build_harness
from ...permissions import Mode
from ..tui.app import HarnessApp
from .headless import run_headless
```

All **four** of these pull in `pydantic_ai` (measured: `permissions` itself does
`from pydantic_ai import DeferredToolRequests, …` at module top, so even `Mode`
drags the whole package). Move all four into `run_default()`, **after** argparse has
parsed/validated args (so `--help` and validation errors exit first). Keep
`argparse`, `asyncio`, `sys`, `Path`, and `...history` (`PromptHistory`,
`default_history_path`) at the top — these are light and `Mode` is **not** referenced
by `_build_parser()` (it's used only at the launch branches, lines 92/98).

Concretely: inside `run_default`, after the worktree handling and before the first
use of `Mode` / `build_harness` / `run_headless` / `HarnessApp`, add the local
imports. Both the headless branch and the TUI branch need `build_harness` and
`Mode`; import those at the point control commits to launching (after `--help`/error
exits), and import `run_headless` / `HarnessApp` in their respective branches.

Result: `marim --help` and arg errors never import `pydantic_ai`; the real launch
still does (unchanged).

## Testing

Add `tests/test_cli_startup.py` with subprocess "import-invariant" tests that run in
a **fresh interpreter** (the only reliable way to assert nothing pulled
`pydantic_ai`, since `sys.modules` is process-global and other tests import it):

1. **`test_router_import_does_not_load_pydantic_ai`** — a subprocess that runs
   `import marim_harness.interfaces.cli.router` then asserts
   `"pydantic_ai" not in sys.modules`. Fails today; passes after change 1.
2. **`test_default_cmd_import_does_not_load_pydantic_ai`** — same, for
   `marim_harness.interfaces.cli.default_cmd`. Fails today; passes after change 2.
3. **`test_config_dispatch_still_works`** — call `router.main` via the public entry
   (monkeypatch `sys.argv` to `["marim", "config"]`, or call the dispatch path) and
   assert it exits 0 and produces config output — a smoke test that lazy dispatch
   didn't break the call contract. (If `config main` needs a workspace/cwd, run it
   in a `tmp_path` cwd.)

Existing `tests/test_config*.py` / `tests/test_models*.py` (if present) must stay
green — the dispatched code is unchanged. The full suite must stay green.

### Test helper shape (subprocess invariant)

```python
import subprocess
import sys


def _imports_pydantic_ai(module: str) -> bool:
    """True if importing `module` in a fresh interpreter pulls in pydantic_ai."""
    code = (
        f"import {module}; import sys; "
        "raise SystemExit(1 if 'pydantic_ai' in sys.modules else 0)"
    )
    return subprocess.run([sys.executable, "-c", code]).returncode == 1


def test_router_import_does_not_load_pydantic_ai():
    assert not _imports_pydantic_ai("marim_harness.interfaces.cli.router")


def test_default_cmd_import_does_not_load_pydantic_ai():
    assert not _imports_pydantic_ai("marim_harness.interfaces.cli.default_cmd")
```

## Risks & mitigations

- **Changed dispatch semantics in router** → keep the exact keyword set and the
  `SystemExit(module.main(argv[1:]))` contract; behavior tests + the smoke test
  guard it.
- **Missed a heavy import left at module top** (e.g., an indirect one) → the
  subprocess invariant tests assert `pydantic_ai` is absent, catching any straggler.
- **Re-introduction later** (someone adds an eager `from .sessions import …`) → the
  invariant tests fail in CI, making the regression loud.

## Build order (plan)

1. Lazy subcommand dispatch in `router.py` + the router import-invariant test.
2. Defer heavy imports in `default_cmd.py` + the default_cmd import-invariant test +
   the config-dispatch smoke test.
