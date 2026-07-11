# Session Scratchpad Directory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the agent a session-specific `/tmp` scratchpad directory — advertised in the system prompt, writable by the file tools, auto-approved in ask mode — so intermediate files stay out of the workspace.

**Architecture:** A pure path helper (`workspace/scratchpad.py`) derives `/tmp/marim-<uid>/<workspace-slug>/<session-id>/scratchpad`; a live getter on `HarnessServices` (like `get_session_id`) exposes it to tools, the approval resolver, and a new instructions closure. The file tools widen their path guard with the scratchpad as an extra root, mirroring the existing `extra_read_roots` skill-dir pattern.

**Tech Stack:** Python ≥3.10, pytest (`uv run pytest`), ruff, pyright. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-11-scratchpad-design.md`

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax (no `Self`, no `except*`).
- Run everything through `uv` (`uv run pytest`, `uv run ruff check src tests`, `uv run pyright`). Never bare `python`/`pip`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- Preserve the long "why" comments near any code you touch; write new ones in the same style.
- Tool docstrings are model-facing product copy — write them accordingly.
- The spec's degradation invariant: any failure to provide the scratchpad (disabled, no session, squatting check, OSError) must degrade to exactly today's behavior — getter returns `None`, no prompt block, no extra root, normal gating.
- One deliberate deviation from the spec: instead of adding `extra_roots` to `resolve_in_workspace` itself, follow the codebase's established pattern (`_safe_read` in `tools/impl/fs.py:77`) and add a `_safe_write` helper that loops extra roots. Same semantics, no signature change to the shared guard.
- A second deviation, user-approved post-review: the main agent's `_scratchpad` instructions block (`runtime/instructions.py`) is gated on `groups.files_write`, not registered unconditionally, and the sub-agent scratchpad line (`workspace/agents.py::subagent_instructions`) is gated on the spawn's actual effective write capability (`"write_file" in effective_tools(...)`, computed once in `SubagentRunner.build` and reused for both tool registration and the prompt), switching to read-only wording when absent. This supersedes Task 5's plain "register the scratchpad line whenever a scratchpad is set" wording — a prompt line advertising a tool the spawn doesn't have makes the model call it and hard-fail (same rationale as the `files_write` gate itself, commit d0d038c).

---

### Task 1: Path helper `workspace/scratchpad.py`

**Files:**
- Create: `src/marim_harness/workspace/scratchpad.py`
- Create: `tests/test_scratchpad.py`

**Interfaces:**
- Produces: `scratchpad_base() -> Path`, `scratchpad_root(workspace_root: Path, session_id: str, base: Path | None = None) -> Path`, `ensure_scratchpad(workspace_root: Path, session_id: str, base: Path | None = None) -> Path | None`. Later tasks import all three from `marim_harness.workspace.scratchpad`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scratchpad.py`:

```python
import os
from pathlib import Path

from marim_harness.session.store import _workspace_dir
from marim_harness.workspace.scratchpad import (
    ensure_scratchpad,
    scratchpad_base,
    scratchpad_root,
)


def test_scratchpad_root_shape():
    root = scratchpad_root(Path("/w/proj"), "sess-1", base=Path("/base"))
    assert root.name == "scratchpad"
    assert root.parent.name == "sess-1"
    slug = root.parent.parent.name
    assert slug.startswith("proj-")
    assert len(slug) == len("proj-") + 12


def test_scratchpad_root_slug_matches_session_store():
    """The workspace slug must stay in lockstep with session storage's naming
    (session/store.py::_workspace_dir), so scratchpads key the same way
    sessions do."""
    ws = Path("/w/proj")
    root = scratchpad_root(ws, "s", base=Path("/base"))
    assert root.parent.parent.name == _workspace_dir(Path("/base"), ws).name


def test_scratchpad_base_is_per_uid():
    base = scratchpad_base()
    assert f"marim-{os.getuid()}" == base.name


def test_ensure_creates_dir_with_private_base(tmp_path):
    base = tmp_path / "b"
    p = ensure_scratchpad(Path("/w/proj"), "s1", base=base)
    assert p is not None and p.is_dir()
    assert (base.stat().st_mode & 0o777) == 0o700


def test_ensure_is_idempotent_and_preserves_files(tmp_path):
    base = tmp_path / "b"
    p1 = ensure_scratchpad(Path("/w/proj"), "s1", base=base)
    (p1 / "note.txt").write_text("hi")
    p2 = ensure_scratchpad(Path("/w/proj"), "s1", base=base)
    assert p2 == p1
    assert (p2 / "note.txt").read_text() == "hi"


def test_ensure_refuses_symlink_base(tmp_path):
    """Classic /tmp squatting: a pre-existing symlink at the base must disable
    the scratchpad, not follow the link."""
    target = tmp_path / "target"
    target.mkdir()
    base = tmp_path / "b"
    base.symlink_to(target)
    assert ensure_scratchpad(Path("/w/proj"), "s1", base=base) is None
```

(Ownership-mismatch can't be simulated without root; the symlink case covers the refuse-and-disable path.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_scratchpad.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.workspace.scratchpad'`

- [ ] **Step 3: Write the implementation**

Create `src/marim_harness/workspace/scratchpad.py`:

```python
"""Session-scoped scratchpad directory under the system temp dir.

The scratchpad lives OUTSIDE the workspace on purpose: it exists so the
agent's intermediate artifacts (temp scripts, staged outputs, analysis
files) don't pollute the project tree or its git status. Pure path
derivation (scratchpad_root) is separated from the impure ensure_scratchpad
(mkdir + /tmp-squatting check) per the repo's pure-helper convention.
See docs/superpowers/specs/2026-07-11-scratchpad-design.md.
"""

import hashlib
import logging
import os
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Bases that already failed the squatting check (or mkdir) and were warned
# about. Module-level so the per-model-request instructions closure and the
# per-tool-call getter don't spam the log with the same warning.
_warned: set[Path] = set()


def scratchpad_base() -> Path:
    """The per-user base every scratchpad lives under (``/tmp/marim-<uid>``).

    Keyed by uid so two users on a shared machine can't collide — and, with
    the ownership check in ensure_scratchpad, can't squat each other's base.
    """
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"marim-{uid}"


def scratchpad_root(
    workspace_root: Path, session_id: str, base: Path | None = None
) -> Path:
    """The scratchpad dir for one session. Pure — no filesystem access.

    ``<base>/<workspace-slug>/<session-id>/scratchpad``. The workspace slug
    (``{name}-{sha256(root)[:12]}``) deliberately matches session storage's
    naming (session/store.py::_workspace_dir) so scratchpads key the same
    way sessions do; a test pins the parity. The ``scratchpad`` leaf leaves
    room for future per-session sidecars in the same directory.
    """
    digest = hashlib.sha256(str(workspace_root).encode()).hexdigest()[:12]
    b = base if base is not None else scratchpad_base()
    return b / f"{workspace_root.name}-{digest}" / session_id / "scratchpad"


def ensure_scratchpad(
    workspace_root: Path, session_id: str, base: Path | None = None
) -> Path | None:
    """Create (if needed) and return the session's scratchpad dir, or None
    when it can't be provided safely — callers treat None as "feature off".

    The base dir is created 0o700 and then verified: it must be a real
    directory (not a symlink) owned by the current uid. A pre-existing
    symlink or foreign-owned dir is classic /tmp squatting — someone
    pre-creating the path to redirect or read our writes — so refuse and
    disable rather than proceed. Each failing base warns once (see _warned).
    """
    b = base if base is not None else scratchpad_base()
    try:
        b.mkdir(mode=0o700, exist_ok=True)
        st = b.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise OSError(f"{b} exists but is not a real directory")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise OSError(f"{b} is owned by uid {st.st_uid}, not {os.getuid()}")
        root = scratchpad_root(workspace_root, session_id, base=b)
        root.mkdir(parents=True, exist_ok=True)
        return root
    except OSError as exc:
        if b not in _warned:
            _warned.add(b)
            logger.warning("scratchpad disabled: %s", exc)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_scratchpad.py -v`
Expected: 6 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/marim_harness/workspace/scratchpad.py tests/test_scratchpad.py
git add src/marim_harness/workspace/scratchpad.py tests/test_scratchpad.py
git commit -m "feat(workspace): session scratchpad path helper with /tmp-squatting guard"
```

---

### Task 2: Write-path guard — `extra_write_roots` in `tools/impl/fs.py`

**Files:**
- Modify: `src/marim_harness/tools/impl/fs.py` (add `_safe_write` next to `_safe_read` at line 77; thread a new param through `write_file` at line 208 and `edit_file` at line 268)
- Test: `tests/test_fs.py` (append)

**Interfaces:**
- Consumes: nothing new (pure fs layer).
- Produces: `fs.write_file(root, path, content, ledger=None, extra_write_roots=())` and `fs.edit_file(root, path, edits, ledger=None, extra_write_roots=())` — both now accept `extra_write_roots: tuple[Path, ...]`. Task 3's tool layer passes the scratchpad there.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fs.py` (match its existing imports; it already imports the `fs` impl module — reuse that import name, shown here as `fs`):

```python
class TestExtraWriteRoots:
    def test_write_file_reaches_extra_root(self, tmp_path):
        ws = tmp_path / "ws"
        scratch = tmp_path / "scratch"
        ws.mkdir()
        scratch.mkdir()
        fs.write_file(ws, str(scratch / "note.txt"), "hi", None, (scratch,))
        assert (scratch / "note.txt").read_text() == "hi"

    def test_write_file_outside_all_roots_refused(self, tmp_path):
        ws = tmp_path / "ws"
        scratch = tmp_path / "scratch"
        ws.mkdir()
        scratch.mkdir()
        with pytest.raises(ModelRetry):
            fs.write_file(
                ws, str(tmp_path / "elsewhere.txt"), "hi", None, (scratch,)
            )

    def test_relative_path_still_lands_in_workspace(self, tmp_path):
        """A relative path must always resolve into the workspace — an extra
        root can never capture it (that would silently divert project writes)."""
        ws = tmp_path / "ws"
        scratch = tmp_path / "scratch"
        ws.mkdir()
        scratch.mkdir()
        fs.write_file(ws, "note.txt", "hi", None, (scratch,))
        assert (ws / "note.txt").exists()
        assert not (scratch / "note.txt").exists()

    def test_symlink_escape_from_extra_root_refused(self, tmp_path):
        ws = tmp_path / "ws"
        scratch = tmp_path / "scratch"
        outside = tmp_path / "outside"
        for d in (ws, scratch, outside):
            d.mkdir()
        (scratch / "link").symlink_to(outside)
        with pytest.raises(ModelRetry):
            fs.write_file(
                ws, str(scratch / "link" / "x.txt"), "hi", None, (scratch,)
            )

    def test_edit_file_reaches_extra_root(self, tmp_path):
        ws = tmp_path / "ws"
        scratch = tmp_path / "scratch"
        ws.mkdir()
        scratch.mkdir()
        (scratch / "note.txt").write_text("hello world")
        fs.edit_file(
            ws,
            str(scratch / "note.txt"),
            [fs.Edit(old_string="hello", new_string="goodbye")],
            None,
            (scratch,),
        )
        assert (scratch / "note.txt").read_text() == "goodbye world"
```

(`pytest` and `ModelRetry` are already imported at the top of `tests/test_fs.py`; add them if this specific file doesn't have them: `import pytest` / `from pydantic_ai import ModelRetry`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_fs.py -k ExtraWriteRoots -v`
Expected: 5 FAIL with `TypeError: write_file() takes ... positional arguments` (the new parameter doesn't exist yet)

- [ ] **Step 3: Implement**

In `src/marim_harness/tools/impl/fs.py`, add below `_safe_read` (after line 90):

```python
def _safe_write(root: Path, path: str, extra_write_roots: tuple[Path, ...]) -> Path:
    """Resolve ``path`` for writing: inside ``root``, or inside one of
    ``extra_write_roots`` (the session scratchpad). Mirrors ``_safe_read``'s
    root-first ordering, which is load-bearing: a relative path always
    resolves against — and lands in — the workspace; only a path the
    workspace guard rejects (an absolute path outside it) may fall through
    to an extra root. An extra root can therefore never capture a relative
    workspace write."""
    try:
        return resolve_in_workspace(root, path)
    except WorkspaceError as exc:
        for extra in extra_write_roots:
            try:
                return resolve_in_workspace(extra, path)
            except WorkspaceError:
                continue
        raise ModelRetry(str(exc)) from exc
```

Change `write_file`'s signature and first line (line 208):

```python
def write_file(
    root: Path,
    path: str,
    content: str,
    ledger: ReadLedger | None = None,
    extra_write_roots: tuple[Path, ...] = (),
) -> str:
    """Create or overwrite a file relative to the workspace root (or, by
    absolute path, inside an extra write root such as the session scratchpad)."""
    p = _safe_write(root, path, extra_write_roots)
```

(The rest of the body is unchanged — the existing `if p.exists(): _require_read_before_write(...)` etc. stay as they are.)

Change `edit_file`'s signature and resolve line (line 268):

```python
def edit_file(
    root: Path,
    path: str,
    edits: list[Edit],
    ledger: ReadLedger | None = None,
    extra_write_roots: tuple[Path, ...] = (),
) -> str:
```

and replace `p = _safe(root, path)` in its body with `p = _safe_write(root, path, extra_write_roots)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_fs.py -v`
Expected: all pass (existing tests unaffected — the new param defaults to `()`)

- [ ] **Step 5: Commit**

```bash
uv run ruff check src/marim_harness/tools/impl/fs.py tests/test_fs.py
git add src/marim_harness/tools/impl/fs.py tests/test_fs.py
git commit -m "feat(fs): extra write roots on write_file/edit_file, mirroring _safe_read"
```

---

### Task 3: Services wiring + tool layer

**Files:**
- Modify: `src/marim_harness/runtime/deps.py` (HarnessServices, after `get_session_id` at line 118)
- Modify: `src/marim_harness/runtime/harness.py` (HarnessConfig ~line 165, `build_services` at line 168, `build_collaborators` before line 337)
- Modify: `src/marim_harness/tools/fs_tools.py` (`_scratch_roots` helper; `read_file`)
- Modify: `src/marim_harness/tools/edit_tools.py` (`write_file`, `edit_file`)
- Test: `tests/test_scratchpad.py` (append)

**Interfaces:**
- Consumes: `ensure_scratchpad` (Task 1), `extra_write_roots` (Task 2).
- Produces: `HarnessServices.get_scratchpad: Callable[[], Path | None] | None` (default None); `HarnessConfig.scratchpad_enabled: bool = True`; `build_services(..., get_scratchpad=...)`; `fs_tools._scratch_roots(ctx) -> tuple[Path, ...]`. Tasks 4–6 read `get_scratchpad` and `scratchpad_enabled`.

- [ ] **Step 1: Write the failing tests**

Merge these imports into the top of `tests/test_scratchpad.py` (ruff enforces import sorting and top-of-file placement — don't paste them mid-file):

```python
from types import SimpleNamespace

import pytest

from marim_harness.runtime.deps import Deps, HarnessServices, WorkspaceConfig
from marim_harness.tools import edit_tools, fs_tools
```

Then append the tests:

```python
def _ctx(ws: Path, scratch: Path | None) -> SimpleNamespace:
    deps = Deps(workspace=WorkspaceConfig(root=ws))
    deps.services = HarnessServices(
        get_scratchpad=(lambda: scratch) if scratch is not None else None
    )
    return SimpleNamespace(deps=deps)


def test_scratch_roots_empty_without_getter(tmp_path):
    assert fs_tools._scratch_roots(_ctx(tmp_path, None)) == ()


def test_scratch_roots_empty_when_getter_returns_none(tmp_path):
    ctx = _ctx(tmp_path, None)
    ctx.deps.services = HarnessServices(get_scratchpad=lambda: None)
    assert fs_tools._scratch_roots(ctx) == ()


@pytest.mark.anyio
async def test_write_tool_reaches_scratchpad(tmp_path):
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    ctx = _ctx(ws, scratch)
    await edit_tools.write_file(ctx, str(scratch / "note.txt"), "hi")
    assert (scratch / "note.txt").read_text() == "hi"


@pytest.mark.anyio
async def test_edit_tool_reaches_scratchpad_after_read(tmp_path):
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    (scratch / "note.txt").write_text("hello")
    ctx = _ctx(ws, scratch)
    # read first: the ReadLedger guard applies to scratchpad files too.
    fs_tools.read_file(ctx, str(scratch / "note.txt"))
    await edit_tools.edit_file(
        ctx,
        str(scratch / "note.txt"),
        [{"old_string": "hello", "new_string": "goodbye"}],
    )
    assert (scratch / "note.txt").read_text() == "goodbye"


def test_read_tool_reaches_scratchpad(tmp_path):
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    (scratch / "data.txt").write_text("payload")
    out = fs_tools.read_file(_ctx(ws, scratch), str(scratch / "data.txt"))
    assert "payload" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_scratchpad.py -v`
Expected: new tests FAIL (`HarnessServices` has no `get_scratchpad`; `fs_tools` has no `_scratch_roots`); Task-1 tests still pass.

- [ ] **Step 3: Implement the deps/services/config plumbing**

`src/marim_harness/runtime/deps.py` — add to `HarnessServices` directly after `get_session_id` (line 118):

```python
    # Returns the active session's scratchpad directory (created on demand),
    # or None when scratchpads are disabled, no session is active, or the dir
    # can't be provided safely (see workspace/scratchpad.py). Live for the
    # same reason as get_session_id: the session id changes on switch. The
    # file tools widen their path guard with it; the approval resolver
    # auto-approves writes into it; an instructions closure advertises it.
    get_scratchpad: Callable[[], Path | None] | None = None
```

(`Path` is already imported in deps.py.)

`src/marim_harness/runtime/harness.py` — three edits:

1. Add to `HarnessConfig` after `groups` (line 165):

```python
    # Session scratchpad master switch. False ⇒ services.get_scratchpad stays
    # None, which degrades everything downstream at once: no prompt block, no
    # extra write root in the file tools, no ask-mode approval bypass.
    scratchpad_enabled: bool = True
```

2. Extend `build_services` (line 168) with the new parameter, passed straight through:

```python
def build_services(
    deps: Deps,
    *,
    lsp: LspManager | None,
    turn_hooks: TurnHooks,
    subagents: SubagentRunner,
    get_session_id: Callable[[], str | None] | None = None,
    get_scratchpad: "Callable[[], Path | None] | None" = None,
) -> HarnessServices:
```

and add `get_scratchpad=get_scratchpad,` to the `HarnessServices(...)` construction inside it. Add `from pathlib import Path` to harness.py's imports if not already present, and `from ..workspace.scratchpad import ensure_scratchpad`.

3. In `build_collaborators`, just before the `build_services(...)` call (line 337), build the live getter and pass it:

```python
    # Live like get_session_id below: a session switch swaps session.store,
    # and the scratchpad must follow the active session. ensure_scratchpad
    # re-mkdirs on every call, so a /tmp cleaned under a resumed session is
    # transparently recreated.
    get_scratchpad = None
    if cfg.scratchpad_enabled:
        def _get_scratchpad() -> Path | None:
            sid = session.store.session_id if session.store is not None else None
            if sid is None:
                return None
            return ensure_scratchpad(deps.workspace.root, sid)
        get_scratchpad = _get_scratchpad
```

and add `get_scratchpad=get_scratchpad,` to the `build_services(...)` call.

- [ ] **Step 4: Implement the tool layer**

`src/marim_harness/tools/fs_tools.py` — add `from pathlib import Path` to imports, then below the module imports:

```python
def _scratch_roots(ctx: RunContext[Deps]) -> tuple[Path, ...]:
    """The session scratchpad as an extra guard root, or () when unavailable.
    The live getter is called per tool call (not captured at registration) so
    the path tracks session switches; any failure inside it already degraded
    to None (see workspace/scratchpad.py), so this never raises."""
    getter = ctx.deps.services.get_scratchpad
    if getter is None:
        return ()
    p = getter()
    return (p,) if p is not None else ()
```

In `read_file` (line 27), widen the read roots and extend the docstring:

```python
    return fs.read_file(
        ctx.deps.workspace.root, path, offset=offset, limit=limit,
        extra_read_roots=skill_roots + _scratch_roots(ctx), ledger=ctx.deps.reads,
    )
```

and append to `read_file`'s docstring (after the skill-directories sentence): `Files in the session scratchpad directory are likewise readable by absolute path.`

`src/marim_harness/tools/edit_tools.py` — add `from .fs_tools import _scratch_roots` to imports. Update both call sites:

```python
    result = await asyncio.to_thread(
        fs.write_file, ctx.deps.workspace.root, path, content, ctx.deps.reads,
        _scratch_roots(ctx),
    )
```

```python
    result = await asyncio.to_thread(
        fs.edit_file, ctx.deps.workspace.root, path, edits, ctx.deps.reads,
        _scratch_roots(ctx),
    )
```

Update both docstrings (model-facing): `write_file` → `"""Create or overwrite a file. `path` is relative to the workspace root, or an absolute path inside the session scratchpad directory."""`; append the same absolute-path sentence to `edit_file`'s docstring.

- [ ] **Step 5: Run tests**

Run: `uv run pytest --no-cov tests/test_scratchpad.py tests/test_fs.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests
git add -A src/marim_harness tests/test_scratchpad.py
git commit -m "feat(runtime): wire session scratchpad through services and the file tools"
```

---

### Task 4: Ask-mode auto-approval for scratchpad writes

**Files:**
- Modify: `src/marim_harness/runtime/permissions.py`
- Modify: `src/marim_harness/runtime/controller.py:739-741` (the `resolve_approvals` call in `_resolve_approval_round`)
- Test: `tests/test_permissions.py` (append)

**Interfaces:**
- Consumes: `HarnessServices.get_scratchpad` (Task 3), `resolve_in_workspace`/`WorkspaceError` from `workspace/fs.py`.
- Produces: `resolve_approvals(requests, mode, request_approval, *, workspace_root: Path | None = None, scratchpad: Path | None = None)` — both new kwargs default to None (existing callers unchanged).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_permissions.py` (reuse its existing `FakeCall`/`FakeRequests`):

```python
@pytest.mark.anyio
async def test_ask_mode_auto_approves_scratchpad_write(tmp_path):
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    reqs = FakeRequests(
        approvals=[FakeCall("c1", "write_file", {"path": str(scratch / "n.txt")})]
    )

    async def never(_call):  # pragma: no cover - must not prompt for scratchpad
        raise AssertionError("scratchpad write must not prompt")

    results = await resolve_approvals(
        reqs, Mode.ask, never, workspace_root=ws, scratchpad=scratch
    )
    assert results.approvals["c1"] is True


@pytest.mark.anyio
async def test_ask_mode_still_prompts_for_workspace_write(tmp_path):
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    reqs = FakeRequests(
        approvals=[FakeCall("c1", "edit_file", {"path": "src/main.py"})]
    )
    seen = []

    async def approve(call):
        seen.append(call.tool_name)
        return True

    await resolve_approvals(
        reqs, Mode.ask, approve, workspace_root=ws, scratchpad=scratch
    )
    assert seen == ["edit_file"]


@pytest.mark.anyio
async def test_ask_mode_bash_never_bypasses_via_scratchpad(tmp_path):
    """Only write_file/edit_file qualify — a bash command mentioning the
    scratchpad path still prompts (its filesystem reach can't be proven)."""
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    reqs = FakeRequests(
        approvals=[FakeCall("c1", "bash", {"command": f"rm -rf {scratch}"})]
    )
    seen = []

    async def approve(call):
        seen.append(call.tool_name)
        return True

    await resolve_approvals(
        reqs, Mode.ask, approve, workspace_root=ws, scratchpad=scratch
    )
    assert seen == ["bash"]


@pytest.mark.anyio
async def test_plan_mode_still_denies_scratchpad_write(tmp_path):
    """Plan mode's no-mutations promise stays absolute — even for the
    scratchpad."""
    from pydantic_ai import ToolDenied

    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    reqs = FakeRequests(
        approvals=[FakeCall("c1", "write_file", {"path": str(scratch / "n.txt")})]
    )
    results = await resolve_approvals(
        reqs, Mode.plan, None, workspace_root=ws, scratchpad=scratch
    )
    assert isinstance(results.approvals["c1"], ToolDenied)


@pytest.mark.anyio
async def test_scratchpad_write_approved_even_without_approver(tmp_path):
    """Headless ask mode (no approver wired): scratchpad writes are
    pre-blessed, so they succeed where everything else is denied."""
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    reqs = FakeRequests(
        approvals=[FakeCall("c1", "write_file", {"path": str(scratch / "n.txt")})]
    )
    results = await resolve_approvals(
        reqs, Mode.ask, None, workspace_root=ws, scratchpad=scratch
    )
    assert results.approvals["c1"] is True


@pytest.mark.anyio
async def test_traversal_out_of_scratchpad_still_prompts(tmp_path):
    """A path that dot-dots from the scratchpad back out must not inherit the
    bypass — resolution is on the resolved target, not the string prefix."""
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    escape = str(scratch / ".." / "outside.txt")
    reqs = FakeRequests(approvals=[FakeCall("c1", "write_file", {"path": escape})])
    seen = []

    async def approve(call):
        seen.append(call.tool_name)
        return True

    await resolve_approvals(
        reqs, Mode.ask, approve, workspace_root=ws, scratchpad=scratch
    )
    assert seen == ["write_file"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_permissions.py -v`
Expected: the 6 new tests FAIL with `TypeError: resolve_approvals() got an unexpected keyword argument 'workspace_root'`; existing tests pass.

- [ ] **Step 3: Implement in `permissions.py`**

Add imports:

```python
from pathlib import Path

from ..workspace.fs import WorkspaceError, resolve_in_workspace
```

Generalize the args extraction (replace `_bash_command`'s body with a shared helper):

```python
def _call_args(call: object) -> dict:
    """Best-effort tool args from an approval call, as a dict. Args arrive as
    a dict or, from some providers, a JSON string."""
    args = getattr(call, "args", None)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return {}
    return args if isinstance(args, dict) else {}


def _bash_command(call: object) -> str:
    """Best-effort extract the ``command`` arg from an approval call."""
    return str(_call_args(call).get("command", ""))
```

Add the scratchpad predicate:

```python
def _is_scratchpad_write(call: object, root: Path, scratchpad: Path) -> bool:
    """True when this approval call is a write_file/edit_file whose target
    resolves inside the scratchpad. Mirrors the tool layer's own resolution
    order (_safe_write in tools/impl/fs.py): the workspace root is tried
    first, so a relative path — which always lands in the workspace — can
    never be mistaken for a scratchpad write; only a path the workspace
    guard rejects may qualify. Resolution chases symlinks and ``..``, so the
    check is on the real target, not a string prefix."""
    if getattr(call, "tool_name", None) not in ("write_file", "edit_file"):
        return False
    path = str(_call_args(call).get("path", ""))
    if not path:
        return False
    try:
        resolve_in_workspace(root, path)
        return False  # a workspace write: normal gating applies
    except WorkspaceError:
        pass
    try:
        resolve_in_workspace(scratchpad, path)
    except WorkspaceError:
        return False
    return True
```

Extend `resolve_approvals`:

```python
async def resolve_approvals(
    requests: DeferredToolRequests,
    mode: Mode,
    request_approval: Callable[[object], Awaitable[DeferredToolApprovalResult | bool]] | None,
    *,
    workspace_root: Path | None = None,
    scratchpad: Path | None = None,
) -> DeferredToolResults:
```

and insert one branch between the `Mode.plan` arm and the `request_approval is None` arm (order matters: plan mode stays a hard deny; the bypass applies only in ask mode, with or without an approver):

```python
        elif (
            scratchpad is not None
            and workspace_root is not None
            and _is_scratchpad_write(call, workspace_root, scratchpad)
        ):
            # Scratchpad writes are pre-blessed in ask mode — the directory
            # exists precisely so intermediate work doesn't prompt (the
            # instructions block advertises exactly that). bash never
            # qualifies: a command's filesystem reach can't be cheaply
            # proven to stay inside the scratchpad.
            results.approvals[call.tool_call_id] = True
```

Update the function docstring's ask-mode sentence to mention the bypass: `ask -> auto-approve write_file/edit_file targeting the scratchpad (when one is wired), otherwise delegate to callback, …`.

- [ ] **Step 4: Update the controller call site**

In `src/marim_harness/runtime/controller.py`, replace lines 739-741:

```python
            get_scratchpad = self.deps.services.get_scratchpad
            deferred_results = await resolve_approvals(
                requests, self.deps.workspace.mode, self.deps.ui.request_approval,
                workspace_root=self.deps.workspace.root,
                scratchpad=get_scratchpad() if get_scratchpad is not None else None,
            )
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest --no-cov tests/test_permissions.py tests/test_approval.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests
git add src/marim_harness/runtime/permissions.py src/marim_harness/runtime/controller.py tests/test_permissions.py
git commit -m "feat(permissions): auto-approve scratchpad writes in ask mode"
```

---

### Task 5: Prompt injection — main agent + sub-agents

**Files:**
- Modify: `src/marim_harness/runtime/instructions.py` (new block function + closure in `register_instructions`)
- Modify: `src/marim_harness/workspace/agents.py:312` (`subagent_instructions`)
- Modify: `src/marim_harness/subagents/runner.py:329-334` (the `Agent(...)` construction in `build`)
- Test: `tests/test_instructions.py`, `tests/test_agents.py` (append)

**Interfaces:**
- Consumes: `HarnessServices.get_scratchpad` (Task 3).
- Produces: `instructions._scratchpad_block(ctx) -> str` (module-level, unit-testable); `subagent_instructions(defn, workspace_root, max_output_chars=None, scratchpad: Path | None = None)`.

- [ ] **Step 1: Write the failing tests**

Merge these imports into the top of `tests/test_instructions.py` (top-of-file, sorted — ruff enforces it):

```python
from pathlib import Path
from types import SimpleNamespace

from marim_harness.runtime.deps import Deps, HarnessServices, WorkspaceConfig
from marim_harness.runtime.instructions import _scratchpad_block
```

Then append the tests:

```python
def _scratch_ctx(getter):
    deps = Deps(workspace=WorkspaceConfig(root=Path("/w")))
    deps.services = HarnessServices(get_scratchpad=getter)
    return SimpleNamespace(deps=deps)


def test_scratchpad_block_renders_path():
    path = Path("/tmp/marim-1/proj-abc/sess/scratchpad")
    text = _scratchpad_block(_scratch_ctx(lambda: path))
    assert str(path) in text
    assert "approval" in text  # advertises the ask-mode bypass


def test_scratchpad_block_absent_without_getter():
    assert _scratchpad_block(_scratch_ctx(None)) == ""


def test_scratchpad_block_absent_when_getter_returns_none():
    assert _scratchpad_block(_scratch_ctx(lambda: None)) == ""
```

Append to `tests/test_agents.py` (its imports of `AgentDef`, `subagent_instructions`, `READ_TOOLS`, and `Path` already exist at the top):

```python
def test_subagent_instructions_mention_scratchpad():
    defn = AgentDef("explore", "d", "Investigate.", READ_TOOLS, "built-in")
    scratch = Path("/tmp/marim-1/proj-abc/sess/scratchpad")
    text = subagent_instructions(defn, Path("/work/space"), scratchpad=scratch)
    assert str(scratch) in text


def test_subagent_instructions_omit_scratchpad_when_none():
    defn = AgentDef("explore", "d", "Investigate.", READ_TOOLS, "built-in")
    text = subagent_instructions(defn, Path("/work/space"))
    assert "scratchpad" not in text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_instructions.py tests/test_agents.py -v`
Expected: new tests FAIL (`ImportError: cannot import name '_scratchpad_block'`; `TypeError: subagent_instructions() got an unexpected keyword argument 'scratchpad'`)

- [ ] **Step 3: Implement the main-agent block**

In `src/marim_harness/runtime/instructions.py`, add a module-level function (near `_memory_index_block`):

```python
def _scratchpad_block(ctx: RunContext[Deps]) -> str:
    """The scratchpad prompt section, or "" when no scratchpad is available
    (disabled, no session, or the dir can't be provided safely — the getter
    already folded all of those to None). Module-level rather than only a
    closure so it is directly unit-testable. The path is stable within a
    session, so the block doesn't churn the prompt cache turn-to-turn."""
    getter = ctx.deps.services.get_scratchpad
    if getter is None:
        return ""
    path = getter()
    if path is None:
        return ""
    return (
        "Scratchpad directory for this session (outside the workspace):\n"
        f"{path}\n\n"
        "Use it, by absolute path, for temporary and intermediate files — "
        "working scripts, staged outputs, analysis artifacts — instead of "
        "writing them into the workspace. write_file/edit_file writes there "
        "do not need approval. It is removed when the session is deleted and "
        "the OS clears it on reboot, so anything worth keeping belongs in "
        "the workspace."
    )
```

Then register it inside `register_instructions`, directly after the `_project_instructions` closure (line 252):

```python
    @agent.instructions
    def _scratchpad(ctx: RunContext[Deps]) -> str:
        return _scratchpad_block(ctx)
```

- [ ] **Step 4: Implement the sub-agent line**

In `src/marim_harness/workspace/agents.py`, extend `subagent_instructions` (line 312):

```python
def subagent_instructions(
    defn: AgentDef, workspace_root, max_output_chars: int | None = None,
    scratchpad: "Path | None" = None,
) -> str:
```

and before the `return base`, add:

```python
    if scratchpad is not None:
        # Shared with the spawning agent (one scratchpad per session), so a
        # sub-agent can hand files back to its parent by writing there.
        base += (
            f"\n\nScratchpad directory for temporary files, shared with the "
            f"agent that spawned you: {scratchpad}. Use it, by absolute path, "
            "for intermediate artifacts instead of the workspace."
        )
```

(Add `from pathlib import Path` to agents.py's imports if it isn't already there.)

In `src/marim_harness/subagents/runner.py`, inside `build` just before the `Agent(...)` construction (line 329), fetch the live path and pass it through:

```python
        get_scratchpad = self.deps.services.get_scratchpad
        scratch = get_scratchpad() if get_scratchpad is not None else None
```

and change the construction's `instructions=` argument to:

```python
            instructions=subagent_instructions(
                defn, instr_root, max_output_chars, scratchpad=scratch
            ),
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest --no-cov tests/test_instructions.py tests/test_agents.py tests/test_agent_instructions.py tests/test_agent_subagents.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
uv run ruff check src tests
git add src/marim_harness/runtime/instructions.py src/marim_harness/workspace/agents.py src/marim_harness/subagents/runner.py tests/test_instructions.py tests/test_agents.py
git commit -m "feat(prompt): advertise the session scratchpad to the main agent and sub-agents"
```

---

### Task 6: Config env knob, bootstrap wiring, session-delete cleanup, docs

**Files:**
- Modify: `src/marim_harness/config/model.py` (field ~line 113, env in `_common_kwargs` ~line 205)
- Modify: `src/marim_harness/runtime/bootstrap.py` (`with_config_overrides`, ~line 133)
- Modify: `src/marim_harness/session/store.py:422-443` (`SessionManager.delete`)
- Modify: `.env.example`, `CLAUDE.md`
- Test: `tests/test_config.py`, `tests/test_session.py` (append)

**Interfaces:**
- Consumes: `HarnessConfig.scratchpad_enabled` (Task 3), `scratchpad_root` (Task 1).
- Produces: `ModelConfig.scratchpad_enabled: bool` read from `MARIM_SCRATCHPAD` (default on).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` (match its existing monkeypatch-env style):

```python
def test_scratchpad_env_defaults_on(monkeypatch):
    monkeypatch.delenv("MARIM_SCRATCHPAD", raising=False)
    from marim_harness.config import load_config

    assert load_config().scratchpad_enabled is True


def test_scratchpad_env_off(monkeypatch):
    monkeypatch.setenv("MARIM_SCRATCHPAD", "0")
    from marim_harness.config import load_config

    assert load_config().scratchpad_enabled is False
```

Append to `tests/test_session.py` (match its existing `SessionManager` construction style — it builds managers against a tmp workspace and `base_dir`):

```python
def test_delete_removes_scratchpad_dir(tmp_path, monkeypatch):
    from marim_harness.session import SessionManager
    from marim_harness.workspace.scratchpad import ensure_scratchpad

    scratch_base = tmp_path / "scratch-base"
    monkeypatch.setattr(
        "marim_harness.workspace.scratchpad.scratchpad_base",
        lambda: scratch_base,
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    manager = SessionManager(ws, base_dir=tmp_path / "sessions")
    store = manager.create()
    scratch = ensure_scratchpad(ws, store.session_id)
    assert scratch is not None and scratch.is_dir()
    manager.delete(store.session_id)
    # the whole per-session dir (the scratchpad's parent) goes with it
    assert not scratch.parent.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_config.py tests/test_session.py -v`
Expected: `test_scratchpad_env_*` FAIL with `AttributeError: 'ModelConfig' object has no attribute 'scratchpad_enabled'`; `test_delete_removes_scratchpad_dir` FAILs on the final assert (delete doesn't remove it yet).

- [ ] **Step 3: Implement**

`src/marim_harness/config/model.py` — add after `forge_enabled` (line 113):

```python
    # Session scratchpad master switch. False ⇒ no scratchpad dir is
    # advertised, writable, or approval-exempt (services.get_scratchpad
    # stays None).
    scratchpad_enabled: bool = True
```

and in `_common_kwargs` (after the `forge_enabled=` line, ~205):

```python
        scratchpad_enabled=_bool_env("MARIM_SCRATCHPAD", True),
```

`src/marim_harness/runtime/bootstrap.py` — add to `with_config_overrides` (after `forge_enabled=cfg.forge_enabled,`):

```python
            scratchpad_enabled=cfg.scratchpad_enabled,
```

`src/marim_harness/session/store.py` — in `delete` (line 422), add to the lazy imports:

```python
        from ..workspace.scratchpad import scratchpad_root
```

and append after the `delete_checkpoint_refs(...)` line:

```python
        # The scratchpad's per-session dir — the PARENT of the `scratchpad`
        # leaf — so any future sidecars in the same dir go with it. Like the
        # rest: best-effort, and /tmp semantics reclaim it on reboot anyway.
        shutil.rmtree(
            scratchpad_root(self.workspace_root, session_id).parent,
            ignore_errors=True,
        )
```

Update `delete`'s docstring list to mention the scratchpad dir.

`.env.example` — add near the other feature switches:

```
# Session scratchpad: a per-session /tmp directory the agent uses for
# intermediate files (advertised in the prompt; writes there skip approval
# in ask mode). 1 (default) or 0.
# MARIM_SCRATCHPAD=1
```

`CLAUDE.md` — in the `workspace/` bullet under "Supporting subsystems", extend the sentence to include the scratchpad, e.g.: `workspace/ — fs primitives, memory (remember/recall), skills, sub-agent specs, git worktrees, snapshots, and the session scratchpad (a per-session /tmp dir for intermediate files: advertised in the prompt, reachable by the file tools as an extra guard root, auto-approved in ask mode, gated by MARIM_SCRATCHPAD).`

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_config.py tests/test_session.py tests/test_bootstrap.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
uv run ruff check src tests
git add -A
git commit -m "feat(config): MARIM_SCRATCHPAD knob, bootstrap wiring, session-delete cleanup"
```

---

### Task 7: Full verification

**Files:** none new.

- [ ] **Step 1: Run the CI sequence locally, in CI's order**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: ruff clean, pyright clean, full suite passes (with coverage on, per pyproject default).

- [ ] **Step 2: Smoke-test the real flow**

Run a headless one-shot in a throwaway dir and confirm the prompt block + write path work end to end:

```bash
mkdir -p /tmp/scratch-smoke && cd /tmp/scratch-smoke && git init -q
uv run --project /home/mateuscmarim/Projects/marim.dev/marim-harness marim -p "Write the single line 'ok' to a file named probe.txt in your scratchpad directory, then read it back and tell me its absolute path." --mode ask
```

Expected: the reply names a path under `/tmp/marim-<uid>/scratch-smoke-*/…/scratchpad/probe.txt`, the file exists with content `ok`, no approval prompt was raised, and `git status` in the workspace is clean. (Adjust the headless flag to the CLI's actual one-shot syntax — check `uv run marim --help` — but the assertion targets stay the same.)

- [ ] **Step 3: Fix anything that surfaced, re-run, commit**

```bash
git add -A
git commit -m "test: scratchpad end-to-end verification fixes"   # only if fixes were needed
```

---

## Self-review notes

- **Spec coverage:** path scheme/lifecycle → Task 1; guard → Task 2 (with the noted `_safe_write` deviation, recorded in Global Constraints); wiring & prompt → Tasks 3, 5; approvals (ask bypass, bash gated, plan denies) → Task 4; sub-agents shared → Task 5; config knob + env + cleanup → Task 6; error-handling degradation → Tasks 1, 3 (None-folding), tested in Tasks 1, 3, 5.
- **Types:** `get_scratchpad: Callable[[], Path | None] | None` is consistent across deps.py, build_services, fs_tools, runner.py, and the controller call site; `extra_write_roots: tuple[Path, ...]` matches between impl and tool layer; `resolve_approvals` kwargs are keyword-only with None defaults so existing tests/callers (test_permissions.py, any other caller) keep working unchanged.
- **claude-cli provider:** untouched by design — none of these code paths run for it.
