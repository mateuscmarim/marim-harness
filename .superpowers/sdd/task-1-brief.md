### Task 1: Relocate `WakeController` to shared runtime

**Files:**
- Move: `src/marim_harness/interfaces/tui/wake.py` → `src/marim_harness/runtime/wake.py`
- Modify: `src/marim_harness/interfaces/tui/app.py:44` (import path)
- Modify: `tests/test_wake.py:3` (import path)

**Interfaces:**
- Produces: `marim_harness.runtime.wake.WakeController` — unchanged class (`should_wake(...)`, `record_auto_turn()`, `reset()`, `depth`, `depth_cap`). This is the single home both `WakeDriver` and the TUI import from after this task.

- [ ] **Step 1: Move the file with git**

Run: `git mv src/marim_harness/interfaces/tui/wake.py src/marim_harness/runtime/wake.py`

- [ ] **Step 2: Update the module docstring**

The class is no longer TUI-specific. Change the opening line of `src/marim_harness/runtime/wake.py` from:

```python
"""Autonomous wake-on-completion policy for the interactive TUI.
```

to:

```python
"""Autonomous wake-on-completion policy, shared by the interactive TUI and the
serve-mode SessionHost.
```

Leave the rest of the docstring and the class body unchanged.

- [ ] **Step 3: Fix the TUI import**

In `src/marim_harness/interfaces/tui/app.py:44`, change:

```python
from .wake import WakeController
```

to:

```python
from ...runtime.wake import WakeController
```

- [ ] **Step 4: Fix the test import**

In `tests/test_wake.py:3`, change:

```python
from marim_harness.interfaces.tui.wake import WakeController
```

to:

```python
from marim_harness.runtime.wake import WakeController
```

- [ ] **Step 5: Confirm nothing else imports the old path**

Run: `grep -rn "interfaces.tui.wake\|from .wake import\|from \.\.wake" src tests`
Expected: no matches.

- [ ] **Step 6: Run the wake tests + lint**

Run: `uv run pytest tests/test_wake.py --no-cov -q && uv run ruff check src/marim_harness/runtime/wake.py src/marim_harness/interfaces/tui/app.py`
Expected: all pass, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(wake): move WakeController policy to runtime/ for sharing

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JiZ12obK27rwbg9iJwdZKx"
```

---

