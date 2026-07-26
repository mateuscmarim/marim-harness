# Offload-Handle Revalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resumed sessions detect offloaded-output files lost to scratchpad cleanup and annotate the stale handles honestly, instead of promising a `read_file` that will fail.

**Architecture:** Three moves per the approved spec (`docs/superpowers/specs/2026-07-26-offload-handle-revalidation-design.md`): (1) `tools/impl/offload.py` exports a machine-recognizable handle envelope — the phrase ``saved to `path` `` — as a regex + helper + gone-note constant, with tripwire tests pinning every producer to it; (2) the one non-conforming producer (`cap_subagent_output`) changes copy, and its two callers switch to a shared pure `spill_target` helper that always puts an **absolute** path in the model-facing note; (3) the existing load-seam history walk in `compaction.py` grows a second detector that **appends** a gone-note to dangling handles (preview survives), threaded with a `base` path for resolving legacy relative handles.

**Tech Stack:** Python 3.10+, pytest, ruff (line 100, C901 ≤ 10), pyright. Use `uv run …` for everything.

## Global Constraints

- The envelope core is exactly ``saved to `absolute-path` `` — backticked path preceded by the words "saved to" (spec §1).
- Regex: ``r"saved to `([^`\n]+)`"`` named `OFFLOAD_HANDLE_RE` in `tools/impl/offload.py` (spec §1).
- Dangling **handles** get `OFFLOAD_GONE_NOTE` *appended* — never replace content; the inline preview must survive (spec §3).
- Dangling **elided pointers** keep existing behavior: replaced with `MASKED_OBSERVATION` (spec §3).
- Revalidation runs ONLY at the load seam (`session/ctrl.py::_load_into_controller`), never per-turn (spec §3 cache rationale).
- Idempotent: content already containing `OFFLOAD_GONE_NOTE` is skipped; one note per part (spec §3).
- Non-string `ToolReturnPart` content is skipped (spec §3).
- `revalidate_elided_pointers` keeps its name and same-object-return contract; gains keyword `base: Path | None = None` (spec §3).
- New handles embed absolute paths; revalidation still resolves relative paths against `base` for pre-existing histories (spec §2).
- Producers do NOT import the regex; conformance is pinned by tripwire tests (spec §1).
- Python ≥3.10 syntax only. Run `uv run ruff check src tests` → `uv run pyright` → tests before each commit.

---

### Task 1: Envelope contract in offload.py + tripwires for the conforming producers

**Files:**
- Modify: `src/marim_harness/tools/impl/offload.py`
- Test: `tests/test_offload.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (later tasks rely on these exact names):
  - `OFFLOAD_HANDLE_RE: re.Pattern[str]`
  - `find_offload_paths(content: str) -> list[str]`
  - `OFFLOAD_GONE_NOTE: str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_offload.py` (it already imports `Path` and the `offload` module — check its header: it uses `from marim_harness.tools.impl import offload` style imports; match whatever import form the file uses; the code below assumes `from marim_harness.tools.impl import offload` plus `from marim_harness.tools.impl.fetch import _offload`):

```python
# --- offload-handle envelope (spec 2026-07-26) --------------------------------


def test_find_offload_paths_extracts_backticked_path():
    text = "blah ⚠️ Large bash result — full output saved to `/tmp/pad/bash-abc.txt`. Read more"
    assert offload.find_offload_paths(text) == ["/tmp/pad/bash-abc.txt"]


def test_find_offload_paths_none_on_plain_text():
    assert offload.find_offload_paths("ordinary output, nothing offloaded") == []
    # An elided-pointer placeholder is NOT a handle.
    assert offload.find_offload_paths(
        "[output elided to save context; full content at /pad/x — read_file it if still needed]"
    ) == []


def test_write_handle_matches_envelope(tmp_path: Path, monkeypatch):
    """Tripwire: a copy edit to _write_handle that breaks the envelope fails here."""
    monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 10)
    result = offload.offload_if_large(
        "line\n" * 50, kind="bash", key="k1", offload_dir=tmp_path
    )
    paths = offload.find_offload_paths(result)
    assert len(paths) == 1
    p = Path(paths[0])
    assert p.is_absolute() and p.exists()
    assert p.read_text() == "line\n" * 50


def test_fetch_offload_matches_envelope(tmp_path: Path):
    """Tripwire: fetch's handle copy stays on the shared envelope."""
    from marim_harness.tools.impl.fetch import _offload

    handle = _offload("# Title\n" + "body\n" * 100, "https://example.com/page", tmp_path)
    paths = offload.find_offload_paths(handle)
    assert len(paths) == 1
    p = Path(paths[0])
    assert p.is_absolute() and p.exists()
```

Note: if `tests/test_offload.py` imports individual names instead of the module, adapt the calls (`offload.find_offload_paths` → `find_offload_paths`) but keep the assertions identical. `monkeypatch.setattr(offload, "_INLINE_CHAR_LIMIT", 10)` requires the module object — import it for that test regardless.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_offload.py -q`
Expected: 2 new envelope tests FAIL with `AttributeError: … has no attribute 'find_offload_paths'`; the two tripwires ERROR the same way. Existing tests still pass.

- [ ] **Step 3: Implement the envelope contract**

In `src/marim_harness/tools/impl/offload.py`, add `import re` to the imports (after `import hashlib`), and add this block after the `_PREVIEW_CHARS` constant (before `_make_preview`):

```python
# --- offload-handle envelope --------------------------------------------------
# Every producer of a "large output saved to a file" handle embeds the path in
# one shared, machine-recognizable form: the words "saved to" followed by the
# absolute path in backticks. Session load revalidates these (compaction.py's
# revalidate_elided_pointers): the scratchpad lives under /tmp, so a resumed
# session can outlive the files its handles point at. Producers keep their own
# natural copy around the core phrase — tripwire tests in test_offload.py /
# test_subagent_tool.py pin each one to the regex, so a wording edit that
# breaks the envelope fails a named test instead of silently disabling
# revalidation.
OFFLOAD_HANDLE_RE = re.compile(r"saved to `([^`\n]+)`")

# Appended (never replacing — the inline preview is real information) to a
# handle whose file no longer exists. Lives here, next to the envelope, so
# producer copy and revalidation copy stay coherent in one module. Also the
# idempotency marker: revalidation skips content that already contains it.
OFFLOAD_GONE_NOTE = (
    "\n\n⚠️ The offloaded file referenced above no longer exists (the "
    "scratchpad was cleaned since this session last ran) — re-run the tool "
    "if you need the full output."
)


def find_offload_paths(content: str) -> list[str]:
    """Every offload-file path embedded in *content* (usually 0 or 1).

    Pure. Matches only the shared envelope — an elided-pointer placeholder
    (compaction.py) uses different copy on purpose and never matches."""
    return OFFLOAD_HANDLE_RE.findall(content)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_offload.py -q`
Expected: PASS (all, including the two tripwires — `_write_handle` and fetch's `_offload` already say ``saved to `path` ``).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/tools/impl/offload.py tests/test_offload.py
git commit -m "feat(offload): machine-recognizable handle envelope + gone-note contract"
```

---

### Task 2: cap_subagent_output on the envelope; absolute spill paths via shared spill_target

**Files:**
- Modify: `src/marim_harness/workspace/agents.py` (cap_subagent_output at ~line 424; add `spill_target` beside it)
- Modify: `src/marim_harness/subagents/runner.py:511-535` (`_cap_output`)
- Modify: `src/marim_harness/workflows/engine.py:411-438` (`_shape`'s spill block)
- Test: `tests/test_subagent_tool.py`

**Interfaces:**
- Consumes: `find_offload_paths` from Task 1 (tests only — producers never import the regex).
- Produces:
  - `spill_target(scratchpad: Path | None, workspace_root: Path, subdir: str, filename: str) -> tuple[str, str | None]` in `workspace/agents.py` — returns `(absolute model-facing path, workspace-relative fallback rel)`; `rel` is `None` when the scratchpad is used.
  - `cap_subagent_output` note copy becomes: `[output capped at N chars — full report saved to `path`]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_tool.py` (it already imports `cap_subagent_output`; add `spill_target` to that same import, and `from marim_harness.tools.impl.offload import find_offload_paths` plus `from pathlib import Path` if absent):

```python
# --- offload-handle envelope conformance (spec 2026-07-26) --------------------


def test_cap_pointer_matches_offload_envelope():
    """Tripwire: the cap note's path is extractable by the shared envelope regex."""
    out = "CONCLUSION first. " + "filler detail. " * 500
    text, spill = cap_subagent_output(out, 300, "/abs/pad/subagent-output/a1.md")
    assert spill == out
    assert find_offload_paths(text) == ["/abs/pad/subagent-output/a1.md"]


def test_spill_target_prefers_scratchpad_absolute(tmp_path):
    pad = tmp_path / "pad"
    root = tmp_path / "ws"
    path, rel = spill_target(pad, root, "subagent-output", "r1.md")
    assert rel is None
    assert Path(path).is_absolute()
    assert path == str(pad / "subagent-output" / "r1.md")


def test_spill_target_fallback_is_absolute_with_workspace_rel(tmp_path):
    root = tmp_path / "ws"
    path, rel = spill_target(None, root, "workflow-output", "wf.json")
    assert rel == ".marim/workflow-output/wf.json"
    assert Path(path).is_absolute()
    assert path == str(root / ".marim" / "workflow-output" / "wf.json")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_tool.py -q`
Expected: `test_cap_pointer_matches_offload_envelope` FAILS (current copy is `full report at path`, no backticks → `find_offload_paths` returns `[]`); the two `spill_target` tests ERROR with ImportError.

- [ ] **Step 3: Implement — agents.py**

In `src/marim_harness/workspace/agents.py::cap_subagent_output`, replace the note line

```python
    note = f"\n\n[output capped at {max_output_chars} chars — full report at {spill_path}]"
```

with

```python
    note = (
        f"\n\n[output capped at {max_output_chars} chars — "
        f"full report saved to `{spill_path}`]"
    )
```

and add `spill_target` directly above `cap_subagent_output` (module already imports nothing heavy; add `from pathlib import Path` to its imports if not present):

```python
def spill_target(
    scratchpad: "Path | None", workspace_root: "Path", subdir: str, filename: str
) -> tuple[str, str | None]:
    """Where an over-budget report spills, preferring the session scratchpad.

    Returns ``(spill_path, rel)``: ``spill_path`` is the ABSOLUTE path that
    goes into the model-facing cap note — absolute so the handle survives a
    resume from any cwd, and so load-seam revalidation (compaction.py) can
    exists-check it without context. ``rel`` is the workspace-relative
    fallback path for ``fs.write_file`` when no scratchpad is available, or
    ``None`` when the scratchpad is used (caller writes there directly).
    Pure — no filesystem access; shared by the sub-agent runner and the
    workflow engine so the two spill paths cannot drift."""
    if scratchpad is not None:
        return str(scratchpad / subdir / filename), None
    rel = f".marim/{subdir}/{filename}"
    return str(workspace_root / rel), rel
```

- [ ] **Step 4: Implement — runner.py**

Replace the body of `SubagentRunner._cap_output` (currently lines 511-535) with:

```python
    def _cap_output(self, output: str, max_output_chars: int | None, ref: str) -> str:
        """Apply a spawner-set output cap to a sub-agent's report. Over budget,
        the full report is spilled to a workspace file and the main agent gets a
        within-budget head + pointer; otherwise the report passes through. The
        cap is lossless — nothing is discarded, only relocated."""
        # Prefer the session scratchpad (session-scoped, auto-cleaned) over
        # the workspace-rooted `.marim/subagent-output/` fallback. The note's
        # path is absolute either way (see spill_target).
        scratchpad = None
        getter = self.deps.services.get_scratchpad
        if getter is not None:
            scratchpad = getter()
        spill_path, rel = spill_target(
            scratchpad, self.deps.workspace.root, "subagent-output", f"{ref}.md"
        )
        text, spill = cap_subagent_output(output, max_output_chars, spill_path)
        if spill is not None:
            if rel is None:
                from pathlib import Path

                from ..atomic_io import atomic_write_text
                dest = Path(spill_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(dest, spill, durable=False)
            else:
                fs.write_file(self.deps.workspace.root, rel, spill)
        return text
```

Add `spill_target` to the existing `from ..workspace.agents import …` import in `runner.py` (search for where `cap_subagent_output` is imported and extend that line). If `Path` is already imported at module top, drop the local `from pathlib import Path`.

- [ ] **Step 5: Implement — engine.py**

In `src/marim_harness/workflows/engine.py::_shape`, replace the spill-path block (currently lines ~413-438, from the `# Prefer the session scratchpad` comment through the `fs.write_file(...)` fallback inside the `if value is None and printed.strip():` branch) with:

```python
        # Prefer the session scratchpad (session-scoped, auto-cleaned) over
        # the workspace-rooted `.marim/workflow-output/` fallback. The note's
        # path is absolute either way (see spill_target).
        scratchpad = None
        getter = self.deps.services.get_scratchpad
        if getter is not None:
            scratchpad = getter()
        spill_path, rel = spill_target(
            scratchpad, self.deps.workspace.root, "workflow-output", f"{name}.json"
        )
        # A None final value with printed output is almost always a script
        # that ended on print(result) instead of a bare `result` expression.
        # The payload -- possibly the product of several expensive sub-agent
        # runs -- already went through print, so return it (with a corrective
        # note) rather than an error that would make the model re-run the
        # whole workflow just to fix its last line.
        if value is None and printed.strip():
            text, spill = cap_subagent_output(printed, MAX_RESULT_CHARS, spill_path)
            if spill is not None:
                if rel is None:
                    from pathlib import Path

                    from ..atomic_io import atomic_write_text
                    dest = Path(spill_path)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(dest, spill, durable=False)
                else:
                    fs.write_file(self.deps.workspace.root, rel, spill)
```

Keep the `return (…)` that follows unchanged. Extend the existing `from ..workspace.agents import cap_subagent_output` import with `spill_target`. Same `Path` note as runner.py. IMPORTANT: `_shape` has a second spill site further down for oversized non-None results — search `_shape` for every other use of the old `spill_path` variable and make sure each one now uses the `spill_path, rel` pair with the same `rel is None` write split (if the only other user just references `spill_path` in a message, it needs no change — it now carries the absolute path, which is the point).

- [ ] **Step 6: Run the covering tests**

Run: `uv run pytest --no-cov tests/test_subagent_tool.py tests/test_workflow_engine.py tests/test_offload.py -q`
(If `tests/test_workflow_engine.py` doesn't exist under that name, run `uv run pytest --no-cov -q -k "workflow"` to cover the engine.)
Expected: PASS. The pre-existing test asserting `".marim/sub/abc.md" in text` still passes — backticks wrap the path but don't alter it.

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/workspace/agents.py src/marim_harness/subagents/runner.py \
        src/marim_harness/workflows/engine.py tests/test_subagent_tool.py
git commit -m "feat(subagents,workflows): cap-note on the offload envelope, absolute spill paths"
```

---

### Task 3: Revalidate dangling handles at the load seam + CHANGELOG

**Files:**
- Modify: `src/marim_harness/compaction.py:313-366` (`_revalidate_parts`, `revalidate_elided_pointers`)
- Modify: `src/marim_harness/session/ctrl.py:430-435` (call site + log wording)
- Modify: `CHANGELOG.md` (Unreleased)
- Test: `tests/test_compaction.py`

**Interfaces:**
- Consumes: `find_offload_paths`, `OFFLOAD_GONE_NOTE` from Task 1; handles produced per Tasks 1-2.
- Produces: `revalidate_elided_pointers(history, exists=os.path.exists, base: Path | None = None)` — same name, same `(new_history, count)` / same-object contract; count now covers annotated handles too.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_compaction.py` (reuse the existing `_tool_return` helper at line ~351; extend the module's `from marim_harness.compaction import …` with `OFFLOAD_GONE_NOTE` — re-exported? No: import it directly with `from marim_harness.tools.impl.offload import OFFLOAD_GONE_NOTE`):

```python
# --- revalidate: offload handles (spec 2026-07-26) ----------------------------


def _handle(path: str) -> str:
    """A realistic offload handle as _write_handle renders it."""
    return (
        "⚠️ Large bash result (30,000 chars, 200 lines) — full output "
        f"saved to `{path}`. Read more with read_file (it paginates) or "
        "grep that path.\n--- preview (first 40 lines) ---\nline one\nline two"
    )


def test_revalidate_annotates_dangling_handle_and_keeps_preview():
    h = _handle("/pad/bash-abc.txt")
    history = [_tool_return("t1", h)]
    new_history, n = revalidate_elided_pointers(history, exists=lambda p: False)
    assert n == 1
    content = new_history[0].parts[0].content
    # Appended, not replaced: the preview survives.
    assert content == h + OFFLOAD_GONE_NOTE
    assert "line one" in content
    # Input never mutated.
    assert history[0].parts[0].content == h


def test_revalidate_leaves_live_handle_untouched():
    history = [_tool_return("t1", _handle("/pad/bash-live.txt"))]
    new_history, n = revalidate_elided_pointers(history, exists=lambda p: True)
    assert n == 0
    assert new_history is history


def test_revalidate_handle_note_is_idempotent():
    history = [_tool_return("t1", _handle("/pad/gone.txt"))]
    once, n1 = revalidate_elided_pointers(history, exists=lambda p: False)
    twice, n2 = revalidate_elided_pointers(once, exists=lambda p: False)
    assert n1 == 1 and n2 == 0
    assert twice is once


def test_revalidate_resolves_relative_handle_against_base(tmp_path):
    # Legacy histories can hold workspace-relative handles; base resolves them.
    live_rel = ".marim/output/live.txt"
    (tmp_path / ".marim" / "output").mkdir(parents=True)
    (tmp_path / live_rel).write_text("payload")
    history = [
        _tool_return("t1", _handle(live_rel)),
        _tool_return("t2", _handle(".marim/output/gone.txt")),
    ]
    new_history, n = revalidate_elided_pointers(history, base=tmp_path)
    assert n == 1
    assert new_history[0].parts[0].content == history[0].parts[0].content
    assert new_history[1].parts[0].content.endswith(OFFLOAD_GONE_NOTE)


def test_revalidate_mixed_pointer_and_handle_counts_both():
    pointer = _elided_pointer("/pad/e/gone.txt")
    h = _handle("/pad/bash-gone.txt")
    history = [_tool_return("t1", pointer), _tool_return("t2", h)]
    new_history, n = revalidate_elided_pointers(history, exists=lambda p: False)
    assert n == 2
    assert new_history[0].parts[0].content == MASKED_OBSERVATION  # replaced
    assert new_history[1].parts[0].content == h + OFFLOAD_GONE_NOTE  # annotated
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_compaction.py -q -k "handle or mixed"`
Expected: the first four FAIL (`n == 0`, content unchanged — no handle detector yet); `test_revalidate_resolves_relative_handle_against_base` FAILS with `TypeError: … unexpected keyword argument 'base'`.

- [ ] **Step 3: Implement — compaction.py**

Add to `compaction.py`'s imports: `from pathlib import Path` (if absent) and

```python
from .tools.impl.offload import OFFLOAD_GONE_NOTE, find_offload_paths
```

Insert this helper above `_revalidate_parts`:

```python
def _annotate_dangling_handles(
    content, exists: Callable[[str], bool], base: Path | None
) -> str | None:
    """*content* with :data:`OFFLOAD_GONE_NOTE` appended when any offload-handle
    path inside it no longer exists, or None when nothing needs annotating.

    Append, never replace: unlike an elided pointer (whose whole content IS the
    placeholder), a handle carries a real inline preview that must survive.
    Idempotent via the note itself — content already annotated is skipped, so
    one note per part even with several dangling paths (the note says
    "referenced above" rather than naming one). Non-absolute paths (legacy
    histories predating absolute spill paths) resolve against *base*."""
    if not isinstance(content, str) or OFFLOAD_GONE_NOTE in content:
        return None
    paths = find_offload_paths(content)
    if not paths:
        return None

    def resolved(p: str) -> str:
        return p if os.path.isabs(p) or base is None else str(base / p)

    if all(exists(resolved(p)) for p in paths):
        return None
    return content + OFFLOAD_GONE_NOTE
```

Replace `_revalidate_parts` (lines 313-330) with:

```python
def _revalidate_parts(
    parts, exists: Callable[[str], bool], base: Path | None
) -> tuple[list | None, int]:
    """Rewrite dangling scratchpad references within one message's parts.

    Two detectors, mutually exclusive by construction (their copy differs on
    purpose): a dangling elided POINTER is replaced with the plain masked
    placeholder (nothing to preserve), a dangling offload HANDLE gets the
    gone-note appended (the preview survives). Returns ``(new_parts,
    rewritten)`` — ``new_parts`` is None when nothing dangled, so the caller
    can skip rebuilding the message."""
    new_parts: list | None = None
    rewritten = 0
    for pidx, part in enumerate(parts):
        if not isinstance(part, ToolReturnPart):
            continue
        replacement: object | None = None
        path = elided_pointer_path(part.content)
        if path is not None:
            if not exists(path):
                replacement = MASKED_OBSERVATION
        else:
            replacement = _annotate_dangling_handles(part.content, exists, base)
        if replacement is None:
            continue
        if new_parts is None:
            new_parts = list(parts)
        new_parts[pidx] = dataclasses.replace(part, content=replacement)
        rewritten += 1
    return new_parts, rewritten
```

Update `revalidate_elided_pointers` (line 333): add the keyword parameter and pass it through — signature becomes

```python
def revalidate_elided_pointers(
    history: list,
    exists: Callable[[str], bool] = os.path.exists,
    base: Path | None = None,
) -> tuple[list, int]:
```

and the inner call becomes `_revalidate_parts(parts, exists, base)`. Extend its docstring's first paragraph with:

```
    Offload HANDLES (the ``saved to `path` `` envelope from
    tools/impl/offload.py) are revalidated in the same walk: a dangling one
    gets OFFLOAD_GONE_NOTE appended — preview preserved — rather than being
    replaced. ``base`` resolves non-absolute handle paths (legacy histories)
    against the workspace root; absolute paths ignore it.
```

- [ ] **Step 4: Implement — ctrl.py call site**

In `src/marim_harness/session/ctrl.py` (line ~430), change

```python
        history, n_dangling = revalidate_elided_pointers(history)
        if n_dangling:
            logger.debug(
                "session load: degraded %d dangling elided pointer(s) to the "
                "plain placeholder (scratchpad file gone)", n_dangling,
            )
```

to

```python
        history, n_dangling = revalidate_elided_pointers(
            history, base=self.deps.workspace.root
        )
        if n_dangling:
            logger.debug(
                "session load: rewrote %d dangling scratchpad reference(s) "
                "(elided pointers masked, offload handles annotated)", n_dangling,
            )
```

Also extend the long comment above the call (lines ~417-429): after "Dangling pointers degrade to the plain masked placeholder ("re-run the tool")." add one sentence:

```
        # Dangling offload HANDLES instead get a gone-note appended (their
        # inline preview is worth keeping); base resolves legacy relative
        # handle paths against the workspace root.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_compaction.py tests/test_session_ctrl.py -q`
(If `tests/test_session_ctrl.py` doesn't exist under that name, find the ctrl tests with `uv run pytest --no-cov -q -k "ctrl or session"`.)
Expected: PASS, including all pre-existing revalidate tests (pointer behavior unchanged; `exists`-only calls still work — `base` defaults to None).

- [ ] **Step 6: CHANGELOG**

Add at the top of the `## [Unreleased]` section of `CHANGELOG.md`:

```markdown
- Resumed sessions now revalidate offloaded-output references at load: a
  handle whose scratchpad file was cleaned up (reboot, tmpfiles aging) gets
  an explicit "file no longer exists — re-run the tool" note appended, with
  the inline preview kept — instead of promising a `read_file` that would
  fail. Sub-agent and workflow spill notes now always carry absolute paths.
```

- [ ] **Step 7: Full gate + commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest -q
git add src/marim_harness/compaction.py src/marim_harness/session/ctrl.py \
        tests/test_compaction.py CHANGELOG.md
git commit -m "feat(session): annotate dangling offload handles at the load seam"
```

---

## Self-Review (done at plan time)

- **Spec coverage:** §1 envelope + tripwires → Task 1 (+ cap tripwire in Task 2); §2 absolute paths → Task 2 (`spill_target`); §3 load-seam revalidation, append semantics, idempotency, base resolution, log wording → Task 3; §4 tripwires → Tasks 1-2; CHANGELOG → Task 3. No gaps.
- **Placeholders:** none; every code step carries the code.
- **Type consistency:** `find_offload_paths(content: str) -> list[str]` (Tasks 1→3), `spill_target(...) -> tuple[str, str | None]` (Task 2 only), `revalidate_elided_pointers(history, exists=..., base: Path | None = None)` (Task 3) — names match across tasks.
- Known judgment call left to implementers' reviewers: `spill_target` lives in `workspace/agents.py` next to `cap_subagent_output` (its only consumers pair them), not in `offload.py`, keeping `workspace/` free of `tools/` imports.
