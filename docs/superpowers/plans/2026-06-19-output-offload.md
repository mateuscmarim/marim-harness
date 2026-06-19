# Large-Output Offload (grep / glob / tree / bash) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `grep`, `glob`, `tree`, and `bash` offload large output to a gitignored file (fetch-style handle + preview) instead of silently truncating, so results are lossless and the turn's context isn't flooded.

**Architecture:** A new shared helper `tools/offload.py` provides `offload_if_large(content, *, kind, key, workspace_root, capped)` and the `MAX_OUTPUT_BYTES` hard ceiling. Each tool builds its full result (bounded by the ceiling), then passes it through the helper: small results return inline unchanged; large ones are written to `.marim/output/<kind>-<digest>.txt` and replaced by a handle + 40-line preview. The old lossy caps are removed.

**Tech Stack:** Python 3.10+, pytest, ruff (line-length 100), pyright. No new dependencies.

## Global Constraints

- Inline/offload boundary: `_INLINE_CHAR_LIMIT = 50_000` chars (same as fetch). At or below it, output is returned inline.
- Hard ceiling: `MAX_OUTPUT_BYTES = 5_000_000`. Producers stop collecting once accumulated output would exceed it, set `capped=True`, and offload what they have.
- Offloaded files: `<workspace_root>/.marim/output/<kind>-<digest>.txt`, `digest = sha256(f"{kind}\0{key}".encode("utf-8")).hexdigest()[:16]`. `kind ∈ {"grep","glob","tree","bash"}`.
- `.marim/output/` must be gitignored.
- When `workspace_root is None` or the write fails, never flood context: clip to `_INLINE_CHAR_LIMIT` with a short note instead of offloading.
- Remove the old lossy caps: `_MAX_GREP_HITS` and `_MAX_TREE_ENTRIES` (fs.py); the middle-drop in the **sync** `run_bash` path (shell.py). `_truncate` stays for the live background `output()` preview only.
- Existing observable behavior for small results must be unchanged: `grep` returns `relpath:line:text` lines or `(no matches)`; `glob_files` returns sorted relpaths or `(no matches)`; `tree` returns the indented listing or `(empty)`; `run_bash` returns `exit N\n<output>`.
- Tests live flat in `tests/`, run with `uv run pytest`. ruff line-length 100; pyright must stay green.
- Tests force the "large" path by `monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", <small>)` rather than generating 50k chars (the helper reads the module global at call time).

---

### Task 1: Shared offload helper + gitignore

**Files:**
- Create: `src/marim_harness/tools/offload.py`
- Modify: `.gitignore`
- Test: `tests/test_offload.py` (new)

**Interfaces:**
- Produces: `MAX_OUTPUT_BYTES: int = 5_000_000`
- Produces: `offload_if_large(content: str, *, kind: str, key: str, workspace_root: Optional[Path], capped: bool = False) -> str` — returns `content` unchanged when `len(content) <= _INLINE_CHAR_LIMIT`; otherwise writes the full content to `.marim/output/<kind>-<digest>.txt` and returns a handle + preview; on no workspace / OSError, clips to `_INLINE_CHAR_LIMIT` with a note.
- Module globals (private, but monkeypatched in tests): `_INLINE_CHAR_LIMIT = 50_000`, `_PREVIEW_LINES = 40`, `_OUTPUT_DIR = (".marim", "output")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_offload.py
from pathlib import Path

from marim_harness.tools import offload


def test_small_content_returned_inline(tmp_path: Path):
    assert offload.offload_if_large("hello", kind="grep", key="x",
                                    workspace_root=tmp_path) == "hello"


def test_large_content_offloaded_to_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 10)
    content = "\n".join(f"line {i}" for i in range(50))
    out = offload.offload_if_large(content, kind="grep", key="pat",
                                   workspace_root=tmp_path)
    # handle, not the raw body
    assert "full output saved to" in out
    assert "grep result" in out
    # the file holds the COMPLETE content
    files = list((tmp_path / ".marim" / "output").glob("grep-*.txt"))
    assert len(files) == 1
    assert files[0].read_text() == content
    # preview shows the first lines
    assert "line 0" in out


def test_digest_is_stable_for_same_kind_and_key(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 1)
    a = offload.offload_if_large("aaa", kind="grep", key="same", workspace_root=tmp_path)
    b = offload.offload_if_large("bbb", kind="grep", key="same", workspace_root=tmp_path)
    # same (kind,key) -> same file path in both handles
    import re
    pa = re.search(r"`([^`]+)`", a).group(1)
    pb = re.search(r"`([^`]+)`", b).group(1)
    assert pa == pb


def test_capped_note_present(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 1)
    out = offload.offload_if_large("data", kind="tree", key="k",
                                   workspace_root=tmp_path, capped=True)
    assert "ceiling" in out.lower()


def test_no_workspace_clips_instead_of_offloading(monkeypatch):
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 10)
    content = "x" * 200
    out = offload.offload_if_large(content, kind="glob", key="k", workspace_root=None)
    assert "saved to" not in out
    assert len(out) < 200
    assert "clipped" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_offload.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.tools.offload'`

- [ ] **Step 3: Create `offload.py`**

```python
# src/marim_harness/tools/offload.py
"""Offload large tool output to a gitignored file instead of flooding context.

A tool builds its full result (bounded by ``MAX_OUTPUT_BYTES``) and passes it
through :func:`offload_if_large`: small results return inline unchanged; large
ones are written under ``.marim/output/`` and replaced by a handle + preview the
agent can page with ``read_file``/``grep``. Mirrors ``fetch``'s offload pattern."""

import hashlib
from pathlib import Path
from typing import Optional

_INLINE_CHAR_LIMIT = 50_000      # at/below this, return inline (~12k tokens)
MAX_OUTPUT_BYTES = 5_000_000     # hard ceiling producers stop collecting at
_PREVIEW_LINES = 40
_OUTPUT_DIR = (".marim", "output")


def _write_handle(content: str, *, kind: str, key: str,
                  workspace_root: Path, capped: bool) -> str:
    digest = hashlib.sha256(f"{kind}\0{key}".encode("utf-8")).hexdigest()[:16]
    rel = Path(*_OUTPUT_DIR, f"{kind}-{digest}.txt")
    dest = workspace_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    lines = content.splitlines()
    preview = "\n".join(lines[:_PREVIEW_LINES])
    cap_note = (
        f"⚠️ Output hit the {MAX_OUTPUT_BYTES:,}-byte ceiling; the file holds what "
        "was collected.\n" if capped else ""
    )
    return (
        f"⚠️ Large {kind} result ({len(content):,} chars, {len(lines):,} lines) — "
        f"full output saved to `{rel.as_posix()}`. Read more with read_file "
        f"(it paginates) or grep that path.\n"
        f"{cap_note}"
        f"--- preview (first {min(_PREVIEW_LINES, len(lines))} lines) ---\n"
        f"{preview}"
    )


def offload_if_large(content: str, *, kind: str, key: str,
                     workspace_root: Optional[Path], capped: bool = False) -> str:
    """Return ``content`` inline when small; otherwise offload to a file and
    return a handle + preview. With no workspace (or on write failure), clip to
    the inline limit instead, so a large result can never flood context."""
    if len(content) <= _INLINE_CHAR_LIMIT:
        return content
    if workspace_root is not None:
        try:
            return _write_handle(content, kind=kind, key=key,
                                 workspace_root=workspace_root, capped=capped)
        except OSError:
            pass
    clipped = content[:_INLINE_CHAR_LIMIT]
    return (
        f"{clipped}\n"
        f"…(output clipped to {_INLINE_CHAR_LIMIT:,} chars; offload unavailable)"
    )
```

- [ ] **Step 4: Add the gitignore entry**

In `.gitignore`, under the existing `.marim/fetch/` line, add:

```
.marim/output/
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_offload.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Lint + types + commit**

```bash
uv run ruff check src/marim_harness/tools/offload.py tests/test_offload.py
uv run pyright src/marim_harness/tools/offload.py
git add src/marim_harness/tools/offload.py tests/test_offload.py .gitignore
git commit -m "feat(tools): shared large-output offload helper"
```
Expected: ruff clean, pyright 0 errors.

---

### Task 2: grep offloads instead of truncating

**Files:**
- Modify: `src/marim_harness/tools/fs.py` (`grep` ~line 184; remove `_MAX_GREP_HITS` ~line 10)
- Test: `tests/test_fs.py`

**Interfaces:**
- Consumes: `offload.offload_if_large`, `offload.MAX_OUTPUT_BYTES`.
- Produces: `grep(root, pattern, path=None) -> str` — full `relpath:line:text` hits inline when small; offloaded (`kind="grep"`, `key=f"{pattern}\0{path or ''}"`) when large; `(no matches)` unchanged; no `(truncated)` marker anymore.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_fs.py
def test_grep_offloads_large_result(tmp_path, monkeypatch):
    from marim_harness.tools import offload
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 50)
    (tmp_path / "big.txt").write_text("\n".join(f"match {i}" for i in range(100)))
    out = fs.grep(tmp_path, "match")
    assert "full output saved to" in out and "grep result" in out
    saved = list((tmp_path / ".marim" / "output").glob("grep-*.txt"))
    assert len(saved) == 1
    # every hit is in the file, nothing truncated
    assert saved[0].read_text().count("big.txt:") == 100
    assert "(truncated)" not in out


def test_grep_small_result_still_inline(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\nbeta")
    out = fs.grep(tmp_path, "alpha")
    assert out == "a.txt:1:alpha"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fs.py::test_grep_offloads_large_result -v`
Expected: FAIL — output is the raw hit list (no "saved to"), because grep still returns inline/truncated.

- [ ] **Step 3: Update `grep` and remove `_MAX_GREP_HITS`**

Add the import near the top of `fs.py` (with the other relative imports):

```python
from .offload import MAX_OUTPUT_BYTES, offload_if_large
```

Delete the line `_MAX_GREP_HITS = 200`.

Replace the `grep` function body with:

```python
def grep(root: Path, pattern: str, path: Optional[str] = None) -> str:
    """Search file contents for a regex, returning `relpath:line:text` hits.
    Large result sets are offloaded to a file (handle + preview) instead of
    flooding the response; collection stops at MAX_OUTPUT_BYTES."""
    rx = re.compile(pattern)
    base = _safe(root, path) if path else root
    files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
    out: list[str] = []
    size = 0
    capped = False
    for f in files:
        if ".worktrees" in f.relative_to(root).parts:
            continue  # skip sibling worktree checkouts
        try:
            resolve_in_workspace(root, str(f.relative_to(root)))
        except (WorkspaceError, ValueError):
            continue
        try:
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if rx.search(line):
                    hit = f"{f.relative_to(root)}:{i}:{line}"
                    out.append(hit)
                    size += len(hit) + 1
                    if size >= MAX_OUTPUT_BYTES:
                        capped = True
                        break
        except (UnicodeDecodeError, OSError):
            continue
        if capped:
            break
    if not out:
        return "(no matches)"
    return offload_if_large(
        "\n".join(out), kind="grep", key=f"{pattern}\0{path or ''}",
        workspace_root=root, capped=capped,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fs.py -k grep -v`
Expected: PASS (new + existing grep tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/marim_harness/tools/fs.py tests/test_fs.py
uv run pyright src/marim_harness/tools/fs.py
git add src/marim_harness/tools/fs.py tests/test_fs.py
git commit -m "feat(grep): offload large result sets instead of truncating"
```

---

### Task 3: glob offloads large match lists

**Files:**
- Modify: `src/marim_harness/tools/fs.py` (`glob_files` ~line 159)
- Test: `tests/test_fs.py`

**Interfaces:**
- Consumes: `offload.offload_if_large`, `offload.MAX_OUTPUT_BYTES` (already imported in Task 2).
- Produces: `glob_files(root, pattern) -> str` — sorted relpaths inline when small; offloaded (`kind="glob"`, `key=pattern`) when large; `(no matches)` unchanged.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_fs.py
def test_glob_offloads_large_result(tmp_path, monkeypatch):
    from marim_harness.tools import offload
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 50)
    for i in range(100):
        (tmp_path / f"f{i}.txt").write_text("x")
    out = fs.glob_files(tmp_path, "*.txt")
    assert "full output saved to" in out and "glob result" in out
    saved = list((tmp_path / ".marim" / "output").glob("glob-*.txt"))
    assert len(saved) == 1
    assert saved[0].read_text().count(".txt") == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fs.py::test_glob_offloads_large_result -v`
Expected: FAIL — glob returns the raw joined list, no "saved to".

- [ ] **Step 3: Update `glob_files`**

Replace the tail of `glob_files` (the `matches.sort()` / return) and add the hard-ceiling guard:

```python
def glob_files(root: Path, pattern: str) -> str:
    """List files under the workspace matching a glob pattern. Large match lists
    are offloaded to a file (handle + preview) instead of flooding the response."""
    try:
        candidates = list(root.glob(pattern))
    except (NotImplementedError, ValueError) as exc:
        raise ModelRetry(
            "invalid glob pattern: use a path relative to the workspace, "
            "no leading '/' or '..'"
        ) from exc
    matches = []
    size = 0
    capped = False
    for p in candidates:
        if not p.is_file():
            continue
        if ".worktrees" in p.relative_to(root).parts:
            continue  # skip sibling worktree checkouts
        rel = str(p.relative_to(root))
        try:
            resolve_in_workspace(root, rel)
        except WorkspaceError:
            continue  # skip matches that escape the workspace root
        matches.append(rel)
        size += len(rel) + 1
        if size >= MAX_OUTPUT_BYTES:
            capped = True
            break
    if not matches:
        return "(no matches)"
    matches.sort()
    return offload_if_large(
        "\n".join(matches), kind="glob", key=pattern,
        workspace_root=root, capped=capped,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fs.py -k glob -v`
Expected: PASS (new + existing glob tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/marim_harness/tools/fs.py tests/test_fs.py
git add src/marim_harness/tools/fs.py tests/test_fs.py
git commit -m "feat(glob): offload large match lists"
```

---

### Task 4: tree offloads instead of truncating

**Files:**
- Modify: `src/marim_harness/tools/fs.py` (`tree` ~line 123, `_walk_tree` ~line 139; remove `_MAX_TREE_ENTRIES` ~line 11)
- Test: `tests/test_fs.py`

**Interfaces:**
- Consumes: `offload.offload_if_large`, `offload.MAX_OUTPUT_BYTES`.
- Produces: `tree(root, path=".", depth=2) -> str` — indented listing inline when small; offloaded (`kind="tree"`, `key=f"{path}\0{depth}"`) when large; `(empty)` unchanged. `_walk_tree(directory, depth, level, lines, size) -> bool` returns True if the hard ceiling was hit.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_fs.py
def test_tree_offloads_large_listing(tmp_path, monkeypatch):
    from marim_harness.tools import offload
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 50)
    for i in range(100):
        (tmp_path / f"f{i:03d}.txt").write_text("x")
    out = fs.tree(tmp_path, ".", depth=1)
    assert "full output saved to" in out and "tree result" in out
    saved = list((tmp_path / ".marim" / "output").glob("tree-*.txt"))
    assert len(saved) == 1
    assert saved[0].read_text().count(".txt") == 100
    assert "(truncated)" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fs.py::test_tree_offloads_large_listing -v`
Expected: FAIL — tree returns the raw listing (capped at 500, no "saved to").

- [ ] **Step 3: Update `tree` / `_walk_tree`, remove `_MAX_TREE_ENTRIES`**

Delete the line `_MAX_TREE_ENTRIES = 500`. Replace `tree` and `_walk_tree`:

```python
def tree(root: Path, path: str = ".", depth: int = 2) -> str:
    """Render an indented directory tree rooted at ``path``, descending up to
    ``depth`` levels. Dirs sort first (with a trailing slash); known-noise dirs
    are listed but not expanded. Large trees are offloaded to a file."""
    base = _safe(root, path)
    if not base.is_dir():
        raise ModelRetry(f"not a directory: {path}")
    lines: list[str] = []
    capped = _walk_tree(base, depth, 0, lines, [0])
    if not lines:
        return "(empty)"
    return offload_if_large(
        "\n".join(lines), kind="tree", key=f"{path}\0{depth}",
        workspace_root=root, capped=capped,
    )


def _walk_tree(directory: Path, depth: int, level: int, lines: list[str],
               size: list[int]) -> bool:
    """Append the entries of ``directory`` to ``lines``, recursing while depth
    allows. ``size`` is a 1-element running byte total; returns True once the
    MAX_OUTPUT_BYTES ceiling is reached so callers stop early."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return False
    entries.sort(key=lambda p: (p.is_file(), p.name.lower()))
    indent = "  " * level
    for entry in entries:
        if size[0] >= MAX_OUTPUT_BYTES:
            return True
        if entry.is_dir():
            line = f"{indent}{entry.name}/"
            lines.append(line)
            size[0] += len(line) + 1
            if entry.name not in _TREE_SKIP_DIRS and level + 1 < depth:
                if _walk_tree(entry, depth, level + 1, lines, size):
                    return True
        else:
            line = f"{indent}{entry.name}"
            lines.append(line)
            size[0] += len(line) + 1
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fs.py -k tree -v`
Expected: PASS (new + existing tree tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/marim_harness/tools/fs.py tests/test_fs.py
uv run pyright src/marim_harness/tools/fs.py
git add src/marim_harness/tools/fs.py tests/test_fs.py
git commit -m "feat(tree): offload large directory listings"
```

---

### Task 5: bash offloads large output (sync + background final)

**Files:**
- Modify: `src/marim_harness/tools/shell.py` (`run_bash` ~line 25; `BashProcess` ~line 59; `start_bash` ~line 106)
- Test: `tests/test_shell.py`

**Interfaces:**
- Consumes: `offload.offload_if_large`, `offload.MAX_OUTPUT_BYTES`.
- Produces: `run_bash(root, command, timeout=..., max_output=...) -> str` — `exit N\n<output>` inline when small; offloaded (`kind="bash"`, `key=command`) when large. `BashProcess.__init__(proc, max_output, root, command)` stores root+command; `BashProcess.wait()` offloads its final result when large; `BashProcess.output()` (live poll) keeps `_truncate` unchanged. `start_bash(root, command, max_output=...)` passes root+command through.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_shell.py
@pytest.mark.anyio
async def test_run_bash_offloads_large_output(tmp_path, monkeypatch):
    from marim_harness.tools import offload
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 100)
    out = await shell.run_bash(tmp_path, "for i in $(seq 1 500); do echo line $i; done")
    assert "full output saved to" in out and "bash result" in out
    saved = list((tmp_path / ".marim" / "output").glob("bash-*.txt"))
    assert len(saved) == 1
    body = saved[0].read_text()
    assert body.startswith("exit 0\n")
    assert body.count("line ") == 500


@pytest.mark.anyio
async def test_run_bash_small_output_inline(tmp_path):
    out = await shell.run_bash(tmp_path, "echo hi")
    assert out == "exit 0\nhi\n"


@pytest.mark.anyio
async def test_background_wait_offloads_but_live_output_truncates(tmp_path, monkeypatch):
    from marim_harness.tools import offload
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 100)
    bp = await shell.start_bash(
        tmp_path, "for i in $(seq 1 500); do echo line $i; done", max_output=80
    )
    final = await bp.wait()
    assert "full output saved to" in final and "bash result" in final
    saved = list((tmp_path / ".marim" / "output").glob("bash-*.txt"))
    assert len(saved) == 1 and saved[0].read_text().count("line ") == 500
    # the live preview path stays bounded by max_output (head+tail truncation)
    assert len(bp.output()) <= 80 + 64  # cap + the "… (N chars truncated) …" marker
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_shell.py -k "offload or inline or background_wait" -v`
Expected: FAIL — `run_bash`/`wait` return middle-dropped text, no "saved to"; `start_bash`/`BashProcess` don't accept `root`/`command`.

- [ ] **Step 3: Update shell.py**

Add the import near the top of `shell.py`:

```python
from .offload import MAX_OUTPUT_BYTES, offload_if_large
```

In `run_bash`, replace the final `text = _truncate(...)` / `return` lines with:

```python
    text = stdout.decode(errors="replace")
    if len(text) > MAX_OUTPUT_BYTES:
        text = text[:MAX_OUTPUT_BYTES]
        capped = True
    else:
        capped = False
    body = f"exit {proc.returncode}\n{text}"
    return offload_if_large(body, kind="bash", key=command,
                            workspace_root=root, capped=capped)
```

Update `BashProcess.__init__` to store root + command:

```python
    def __init__(self, proc: asyncio.subprocess.Process, max_output: int,
                 root: Path, command: str) -> None:
        self._proc = proc
        self._max_output = max_output
        self._root = root
        self._command = command
        self._buffer: list[str] = []
```

Keep `output()` exactly as-is (still `_truncate` — the live streaming preview). Replace `wait()`'s return:

```python
    async def wait(self) -> str:
        if self._proc.stdout is not None:
            while True:
                chunk = await self._proc.stdout.readline()
                if not chunk:
                    break
                self._buffer.append(chunk.decode(errors="replace"))
        await self._proc.wait()
        text = "".join(self._buffer)
        if len(text) > MAX_OUTPUT_BYTES:
            text = text[:MAX_OUTPUT_BYTES]
            capped = True
        else:
            capped = False
        body = f"exit {self._proc.returncode}\n{text}"
        return offload_if_large(body, kind="bash", key=self._command,
                                workspace_root=self._root, capped=capped)
```

Update `start_bash` to pass root + command:

```python
    return BashProcess(proc, max_output, root, command)
```

(`run_bash` already receives `root`; the `_truncate` helper stays for `output()`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_shell.py -v`
Expected: PASS (new + existing shell tests).

- [ ] **Step 5: Lint + types + commit**

```bash
uv run ruff check src/marim_harness/tools/shell.py tests/test_shell.py
uv run pyright src/marim_harness/tools/shell.py
git add src/marim_harness/tools/shell.py tests/test_shell.py
git commit -m "feat(bash): offload large output (sync + background final result)"
```

---

### Task 6: Refactor fetch onto the shared helper

**Files:**
- Modify: `src/marim_harness/tools/fetch.py` (`_offload` ~line 93)
- Test: `tests/test_fetch.py`

**Interfaces:**
- Consumes: a new `offload.write_preview_file(content, *, rel, workspace_root) -> tuple[str, str, int]` added in this task (the shared file-write + preview core).
- Produces: `fetch_url(...)` — observable output UNCHANGED (same title + `Fetched <url>` header, same "saved to" handle and preview). This task is purely DRY; if it risks changing fetch's output format, SKIP it and report DONE_WITH_CONCERNS.

- [ ] **Step 1: Add a regression test pinning fetch's current handle shape**

```python
# add to tests/test_fetch.py
@pytest.mark.anyio
async def test_fetch_offload_handle_has_title_and_saved_path(tmp_path, monkeypatch):
    from marim_harness.tools import fetch
    monkeypatch.setattr(fetch, "_INLINE_CHAR_LIMIT", 20)
    body = "# My Title\n" + "\n".join(f"para {i}" for i in range(50))
    out = fetch._offload(body, "https://example.com/x", tmp_path)
    assert out.startswith("# My Title")
    assert "Fetched https://example.com/x" in out
    assert "saved to" in out
    saved = list((tmp_path / ".marim" / "fetch").glob("*.md"))
    assert len(saved) == 1 and saved[0].read_text() == body
```

- [ ] **Step 2: Run test to verify it passes against current code**

Run: `uv run pytest tests/test_fetch.py::test_fetch_offload_handle_has_title_and_saved_path -v`
Expected: PASS (this pins the existing behavior before refactor).

- [ ] **Step 3: Refactor `fetch._offload` to reuse the shared writer**

Add to `offload.py` a small public helper that writes a file and returns its preview pieces, so fetch can wrap it with its own header. Add:

```python
def write_preview_file(content: str, *, rel: Path, workspace_root: Path) -> tuple[str, str, int]:
    """Write *content* to ``workspace_root/rel`` and return (rel_posix, preview,
    line_count) for the caller to format into a handle."""
    dest = workspace_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    lines = content.splitlines()
    preview = "\n".join(lines[:_PREVIEW_LINES])
    return rel.as_posix(), preview, len(lines)
```

Then rewrite `fetch._offload` to call it (keeping fetch's exact output text):

```python
def _offload(body: str, url: str, workspace_root: Path) -> str:
    from .offload import write_preview_file, _PREVIEW_LINES as _PL
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    rel = Path(*_FETCH_DIR, f"{digest}.md")
    rel_posix, preview, n_lines = write_preview_file(body, rel=rel,
                                                     workspace_root=workspace_root)
    return (
        f"# {_title_of(body, url)}\n"
        f"Fetched {url}\n\n"
        f"⚠️ Large page ({len(body):,} chars, {n_lines:,} lines) — full content "
        f"saved to `{rel_posix}`. Read more with read_file (it paginates) or grep "
        f"that path for what you need.\n\n"
        f"--- preview (first {min(_PL, n_lines)} lines) ---\n"
        f"{preview}"
    )
```

(Keep fetch's own `_PREVIEW_LINES`/`_FETCH_DIR`/`_INLINE_CHAR_LIMIT` constants as they are; only the file-write + preview is shared.)

- [ ] **Step 4: Run the fetch tests to verify output is unchanged**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: PASS (the pinning test from Step 1 plus the existing offload tests still pass — output format identical).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/marim_harness/tools/fetch.py src/marim_harness/tools/offload.py tests/test_fetch.py
uv run pyright src/marim_harness/tools/fetch.py src/marim_harness/tools/offload.py
git add src/marim_harness/tools/fetch.py src/marim_harness/tools/offload.py tests/test_fetch.py
git commit -m "refactor(fetch): reuse shared offload file-writer (output unchanged)"
```

---

## Final verification

- [ ] **Run the full suite, lint, and types**

```bash
uv run pytest
uv run ruff check src tests
uv run pyright
```
Expected: all green.
