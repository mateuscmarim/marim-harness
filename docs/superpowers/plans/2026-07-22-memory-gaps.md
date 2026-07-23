# Memory Gaps (forget, wikilinks, guidance polish) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give marim's native memory the three capabilities identified in the
spec (`docs/superpowers/specs/2026-07-22-memory-gaps-design.md`): a gated
`forget` tool for deleting memories, the `[[wikilink]]` convention with a link
footer in `recall`, and store-hygiene guidance in docstrings/policy strings.

**Architecture:** All file mechanics stay in `workspace/memory.py` (the single
fail-soft writer module); the tool layer (`tools/memory_tools.py`) remains thin
`ctx.deps`-unwrapping wiring; registration and gating live in
`tools/provider.py`; sub-agent/group name sets in `tools/names.py`; instruction
text in `runtime/instructions.py`.

**Tech Stack:** Python 3.10+ (no 3.11+ syntax), pydantic-ai, pytest. Run
everything through `uv` (`uv run pytest`, `uv run ruff check src tests`,
`uv run pyright`). Never bare `python`/`pip`.

## Global Constraints

- `requires-python >= 3.10` — no `match`-only-3.11 features, no `Self`, etc.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; complexity cap 10.
- Nothing in `workspace/memory.py` may raise into a turn — fail soft (log +
  return a caller-checkable value), matching the module docstring's promise.
- Tool docstrings are model-facing product text — keep them tight and imperative.
- Tests use `uv run pytest --no-cov <file>` for fast single-file runs.
- Commit after each task. Add ONLY the files you changed (never `git add -A`).
- The suite has one known-flaky test unrelated to this work
  (`tests/test_app.py::test_fresh_log_top_aligned_then_anchors_on_overflow`);
  if it fails in a full run, rerun it alone before suspecting your change.

---

### Task 1: `delete_memory` in `workspace/memory.py`

**Files:**
- Modify: `src/marim_harness/workspace/memory.py` (module currently ends at
  `save_memory`, line 171)
- Test: `tests/test_memory.py` (append at end)

**Interfaces:**
- Consumes: existing `MemoryScope`, `_slugify`, `file_lock`, `atomic_write_text`,
  `_INDEX_FILE`.
- Produces: `delete_memory(scope: MemoryScope, name: str) -> bool` — Task 2's
  `forget` tool calls this. Also hoists the index-entry regex to module level as
  `_ENTRY_LINK_RE` (used by both `_upsert_index_line` and the new
  `_remove_index_line`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_memory.py` (it already has `import os`, `import pytest`,
`from pathlib import Path`, and imports `memory`; check its header and reuse):

```python
def _save(sc, name: str, hook: str = "d", body: str = "b") -> None:
    memory.save_memory(
        sc, name=name, description=hook, mem_type="project", body=body, title=name
    )


def test_delete_memory_removes_file_and_index_line(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    _save(sc, "Build tool")
    _save(sc, "Other fact")
    assert memory.delete_memory(sc, "Build tool") is True
    assert not (sc.root / "build-tool.md").exists()
    index = (sc.root / "MEMORY.md").read_text()
    assert "build-tool.md" not in index
    # The other entry's line survives untouched.
    assert "other-fact.md" in index


def test_delete_memory_resolves_title_or_slug(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    _save(sc, "Usuário favorito")
    # Title and slug both slugify to the same stored filename.
    assert memory.delete_memory(sc, "usuario-favorito") is True
    assert not (sc.root / "usuario-favorito.md").exists()


def test_delete_memory_missing_returns_false(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    assert memory.delete_memory(sc, "never-saved") is False


def test_delete_memory_spares_entry_whose_hook_links_to_it(tmp_path: Path):
    """Deleting `auth` must not clobber a DIFFERENT entry whose hook text
    mentions `auth.md` — matching is by the line's own link target."""
    sc = memory.project_scope(tmp_path)
    _save(sc, "auth")
    _save(sc, "tokens", hook="see [link](auth.md) for details")
    assert memory.delete_memory(sc, "auth") is True
    index = (sc.root / "MEMORY.md").read_text()
    assert "tokens.md" in index
    assert "see [link](auth.md)" in index  # hook untouched


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores mode bits; chmod cannot provoke the failure"
)
def test_delete_memory_fails_soft_on_unwritable_dir(tmp_path: Path):
    """Module contract: nothing raises into a turn. An index that can't be
    rewritten (read-only dir) must log + return False, not propagate OSError."""
    sc = memory.project_scope(tmp_path)
    _save(sc, "x")
    sc.root.chmod(0o500)
    try:
        assert memory.delete_memory(sc, "x") is False
    finally:
        sc.root.chmod(0o700)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_memory.py -k delete_memory -v`
Expected: 5 failures/errors with `AttributeError: module ... has no attribute 'delete_memory'`

- [ ] **Step 3: Implement**

In `src/marim_harness/workspace/memory.py`, first hoist the entry regex.
Replace (inside `_upsert_index_line`):

```python
    # Match this entry by its OWN link target — the first `](…md)` of an index
    # line — not by a bare substring. A plain ``"](slug.md)" in raw`` test would
    # also fire on a *different* entry whose hook text happens to mention
    # ``slug.md`` (e.g. "see [link](auth.md)"), clobbering the wrong line.
    entry_link = re.compile(r"^- \[.*?\]\((?P<slug>[^)]+)\.md\)")
```

with a module-level constant placed right after `_VALID_TYPES` (and update the
two `entry_link.match(raw)` uses in `_upsert_index_line` to `_ENTRY_LINK_RE.match(raw)`):

```python
# Matches an index entry by its OWN link target — the first `](…md)` of an index
# line — never by bare substring. A plain ``"](slug.md)" in raw`` test would
# also fire on a *different* entry whose hook text happens to mention
# ``slug.md`` (e.g. "see [link](auth.md)"), hitting the wrong line. Shared by
# the upsert (refresh-in-place) and delete (drop-the-line) paths so the two
# can't disagree about what "this entry's line" means.
_ENTRY_LINK_RE = re.compile(r"^- \[.*?\]\((?P<slug>[^)]+)\.md\)")
```

Then append after `save_memory`:

```python
def _remove_index_line(scope: MemoryScope, *, slug: str) -> None:
    """Drop ``slug``'s pointer from ``MEMORY.md``, preserving every other line.
    Same advisory-lock + atomic-write discipline as ``_upsert_index_line`` so a
    concurrent save can't resurrect the deleted entry's line or lose its own."""
    path = scope.root / _INDEX_FILE
    with file_lock(path):
        if not path.exists():
            return
        kept = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            m = _ENTRY_LINK_RE.match(raw)
            if m and m.group("slug") == slug:
                continue
            kept.append(raw)
        text = "\n".join(kept)
        atomic_write_text(path, text + "\n" if text else "")


def delete_memory(scope: MemoryScope, name: str) -> bool:
    """Delete the memory named ``name`` (title or slug — both slugify to the
    stored filename) and drop its index line. Returns True when the file
    existed and was removed, False when there was nothing to delete or the
    delete failed. Per the module docstring, nothing raises into a turn: OSErrors
    are logged and folded into False, matching save_memory's fail-soft style."""
    slug = _slugify(name)
    path = scope.root / f"{slug}.md"
    try:
        if not path.is_file():
            return False
        path.unlink()
        _remove_index_line(scope, slug=slug)
    except OSError as exc:
        logger.debug("failed to delete memory %s (%s): %s", path, scope.name, exc)
        return False
    logger.debug("deleted memory %s (%s)", path, scope.name)
    return True
```

Note on the fail-soft test: with the dir at `0o500`, `path.unlink()` raises
`PermissionError` (an `OSError`) before the index is touched — the except
clause returns `False`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_memory.py -v`
Expected: all PASS (the pre-existing tests in the file must stay green — the
regex hoist changed `_upsert_index_line` internals).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/marim_harness/workspace/memory.py tests/test_memory.py
uv run pyright
git add src/marim_harness/workspace/memory.py tests/test_memory.py
git commit -m "feat(memory): delete_memory with locked index-line removal"
```

---

### Task 2: gated `forget` tool

**Files:**
- Modify: `src/marim_harness/tools/memory_tools.py` (append after `recall`)
- Modify: `src/marim_harness/tools/provider.py:137-163` (`_register_action_tools`)
- Modify: `src/marim_harness/tools/names.py:50` (`TOOL_GROUPS["memory"]`)
- Modify: `src/marim_harness/interfaces/tui/widgets/tool_summary.py:30-38,169`
- Test: `tests/test_memory_tool.py`, `tests/test_tool_summary.py`

**Interfaces:**
- Consumes: `delete_memory(scope, name) -> bool` from Task 1; existing
  `resolve_scope(ctx, which)` in `memory_tools.py`.
- Produces: tool function `forget(ctx: RunContext[Deps], name: str,
  scope: Literal["project", "global"] = "project") -> str`, registered on the
  main agent with `requires_approval=True`. No later task depends on it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_memory_tool.py` (reuses its existing imports plus two new
ones — add `from types import SimpleNamespace` to the import block and note the
file already imports `_make_deps`, `Mode`, `BuiltinToolProvider`):

```python
def test_forget_deletes_memory_and_index_line(tmp_path: Path):
    from marim_harness.tools.memory_tools import forget
    from marim_harness.workspace import memory

    sc = memory.project_scope(tmp_path)
    memory.save_memory(sc, name="Build tool", description="d",
                       mem_type="project", body="b", title="Build tool")
    ctx = SimpleNamespace(
        deps=SimpleNamespace(workspace=SimpleNamespace(memory_root=None, root=tmp_path))
    )
    result = forget(ctx, name="Build tool")
    assert "deleted" in result.lower()
    assert not (sc.root / "build-tool.md").exists()
    assert "build-tool.md" not in (sc.root / "MEMORY.md").read_text()


def test_forget_missing_memory_returns_notice(tmp_path: Path):
    from marim_harness.tools.memory_tools import forget

    ctx = SimpleNamespace(
        deps=SimpleNamespace(workspace=SimpleNamespace(memory_root=None, root=tmp_path))
    )
    result = forget(ctx, name="never-saved")
    assert "no project memory" in result.lower()


def test_forget_requires_approval():
    """Pins requires_approval=True on the registration itself (provider.py) —
    deletion is the one irreversible memory operation, so unlike remember/recall
    it must route through resolve_approvals (ask prompts, plan denies)."""
    agent = _agent()
    tool = agent._function_toolset.tools["forget"]
    assert tool.requires_approval is True


def test_forget_scope_is_constrained_to_two_values():
    schema = _tool_schema("forget")
    scope = schema["properties"]["scope"]
    assert scope.get("enum") == ["project", "global"]


def test_forget_is_not_grantable_to_subagents():
    from marim_harness.tools.names import SUBAGENT_TOOLS, TOOL_GROUPS

    assert "forget" not in SUBAGENT_TOOLS
    # But it IS part of the builder's memory composition group.
    assert "forget" in TOOL_GROUPS["memory"]
```

And append to `tests/test_tool_summary.py` (it imports `summarize` and
`ToolSummary` at the top — follow the file's existing style):

```python
def test_forget_global_scope_badge():
    s = summarize("forget", {"name": "my-fact", "scope": "global"})
    assert s.label == "Forget"
    assert s.target == "my-fact"
    assert s.badges == ("global",)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_memory_tool.py tests/test_tool_summary.py -k forget -v`
Expected: FAIL — `ImportError`/`AttributeError` for `forget`, `KeyError: 'forget'`
for the registration test, empty badge tuple for the summary test.

- [ ] **Step 3: Implement**

`src/marim_harness/tools/memory_tools.py` — change the import line to include
`delete_memory`:

```python
from ..workspace.memory import (
    MemoryScope, delete_memory, global_scope, project_scope, read_memory, save_memory,
)
```

Append after `recall`:

```python
def forget(
    ctx: RunContext[Deps], name: str,
    scope: Literal["project", "global"] = "project",
) -> str:
    """Permanently delete a saved memory by `name` (its title or slug, as
    shown in the memory index). Use only when a memory is wrong or
    obsolete; to correct or refresh a fact, prefer remember with the
    same title, which updates the entry in place. `scope` is "project"
    (default) or "global". Check the memory index first so you delete
    the entry you mean — deletion cannot be undone."""
    sc = resolve_scope(ctx, "global" if scope == "global" else "project")
    if delete_memory(sc, name):
        return f"Deleted {sc.name} memory {name!r}."
    return (
        f"No {sc.name} memory named {name!r} to delete "
        "(check the memory index; or its directory is not writable)."
    )
```

`src/marim_harness/tools/provider.py` — in `_register_action_tools`, insert
after the `if g.net:` block (before `if g.tasks:`):

```python
    # forget is the memory group's one gated tool: deletion is the only
    # irreversible memory operation, so while remember/recall register
    # ungated in _register_read_tools, forget routes through
    # resolve_approvals like write/edit/bash — auto runs it un-prompted,
    # ask prompts per call, plan denies it.
    if g.memory:
        agent.tool(requires_approval=True)(memory_tools.forget)
```

`src/marim_harness/tools/names.py:50` — change the memory group line to:

```python
    "memory": frozenset({"remember", "recall", "forget"}),
```

`src/marim_harness/interfaces/tui/widgets/tool_summary.py` — two edits:
in `_TARGET_ARG` (line 37) change the memory line to:

```python
    "remember": "title", "recall": "name", "forget": "name",
```

and in `_badges` (line 169) change the scope-badge test to:

```python
    if tool_name in ("remember", "recall", "forget") and args.get("scope") == "global":
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_memory_tool.py tests/test_tool_summary.py tests/test_provider.py -v`
Expected: all PASS. `test_provider.py` matters: `test_each_group_toggles_exactly_its_tools`
verifies that toggling `memory=False` now removes exactly
`{remember, recall, forget}`, which only holds if the registration landed under
`if g.memory:` and the names group was updated — both halves of Step 3.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests
uv run pyright
git add src/marim_harness/tools/memory_tools.py src/marim_harness/tools/provider.py \
        src/marim_harness/tools/names.py \
        src/marim_harness/interfaces/tui/widgets/tool_summary.py \
        tests/test_memory_tool.py tests/test_tool_summary.py
git commit -m "feat(memory): gated forget tool for deleting memories"
```

---

### Task 3: `[[wikilinks]]` — extract, annotate, surface in recall

**Files:**
- Modify: `src/marim_harness/workspace/memory.py` (append after `delete_memory`)
- Modify: `src/marim_harness/tools/memory_tools.py` (`recall` body + `remember`
  docstring)
- Test: `tests/test_memory.py`, `tests/test_memory_tool.py`

**Interfaces:**
- Consumes: `_slugify`, `MemoryScope` (memory.py internals); the `_save` test
  helper Task 1 added at the end of `tests/test_memory.py`.
- Produces: `extract_links(body: str) -> list[str]` and
  `annotate_links(scope: MemoryScope, body: str) -> str` in
  `workspace/memory.py`; `recall` returns the annotated body. No later task
  depends on these names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_memory.py`:

```python
def test_extract_links_basic_order_and_dedup():
    body = "See [[auth-flow]] and [[tokens]]; [[auth-flow]] again."
    assert memory.extract_links(body) == ["auth-flow", "tokens"]


def test_extract_links_none():
    assert memory.extract_links("no links here [not one](x.md)") == []
    assert memory.extract_links("") == []


def test_extract_links_strips_and_skips_empty():
    assert memory.extract_links("[[ padded name ]] and [[]]") == ["padded name"]


def test_annotate_links_footer_distinguishes_saved_from_unwritten(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    _save(sc, "auth-flow")
    body = "Uses [[auth-flow]]; see also [[token-rotation]]."
    out = memory.annotate_links(sc, body)
    assert out.startswith(body)
    footer = out[len(body):]
    assert "saved: auth-flow" in footer
    assert "not yet written: token-rotation" in footer


def test_annotate_links_no_links_returns_body_unchanged(tmp_path: Path):
    sc = memory.project_scope(tmp_path)
    assert memory.annotate_links(sc, "plain body") == "plain body"


def test_annotate_links_resolves_title_style_links_via_slug(tmp_path: Path):
    """A link written as the memory's TITLE ("Auth flow") must match the
    slug-named file (auth-flow.md) — existence is checked by slugifying."""
    sc = memory.project_scope(tmp_path)
    _save(sc, "Auth flow")
    out = memory.annotate_links(sc, "see [[Auth flow]]")
    assert "saved: Auth flow" in out
```

Append to `tests/test_memory_tool.py`:

```python
def test_recall_appends_link_footer(tmp_path: Path):
    from marim_harness.workspace import memory

    sc = memory.project_scope(tmp_path)
    memory.save_memory(sc, name="deploy", description="d", mem_type="project",
                       body="After [[build]] run the deploy.", title="deploy")
    agent = _agent()
    model, captured = _call_recall({"name": "deploy", "scope": "project"})
    with agent.override(model=model):
        agent.run_sync("recall", deps=_make_deps(tmp_path, mode=Mode.ask))
    assert "not yet written: build" in captured["ret"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_memory.py tests/test_memory_tool.py -k "extract_links or annotate_links or link_footer" -v`
Expected: FAIL with `AttributeError: ... no attribute 'extract_links'` etc.

- [ ] **Step 3: Implement**

`src/marim_harness/workspace/memory.py` — add near `_ENTRY_LINK_RE`:

```python
# ``[[name]]`` wikilinks inside a memory body. Names may be titles or slugs
# (annotate_links slugifies either way); brackets inside brackets are not
# supported — the convention is flat links, mirroring Claude Code's memory.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
```

Append after `delete_memory`:

```python
def extract_links(body: str) -> list[str]:
    """The distinct ``[[name]]`` wikilink targets in a memory body, in first-
    appearance order (whitespace-trimmed; empty links skipped)."""
    seen: dict[str, None] = {}
    for m in _WIKILINK_RE.finditer(body or ""):
        target = m.group(1).strip()
        if target:
            seen.setdefault(target, None)
    return list(seen)


def annotate_links(scope: MemoryScope, body: str) -> str:
    """Return ``body``, plus — when it contains ``[[name]]`` links — a one-line
    footer telling the model which linked memories are saved and which are still
    unwritten. Existence is checked by slugifying each link and testing for its
    ``<slug>.md`` file (not the index, which could be stale). A dangling link is
    not an error: per the convention it marks a fact worth writing later."""
    links = extract_links(body)
    if not links:
        return body
    saved = [t for t in links if (scope.root / f"{_slugify(t)}.md").is_file()]
    unwritten = [t for t in links if t not in saved]
    parts = []
    if saved:
        parts.append("saved: " + ", ".join(saved))
    if unwritten:
        parts.append("not yet written: " + ", ".join(unwritten))
    return body + "\n\nLinked memories — " + "; ".join(parts) + ". Read saved ones with recall."
```

`src/marim_harness/tools/memory_tools.py` — extend the workspace.memory import
with `annotate_links`, and change `recall`'s last line from
`return read_memory(sc, name)` to:

```python
    return annotate_links(sc, read_memory(sc, name))
```

(The "no memory named" notice contains no `[[links]]`, so it passes through
unchanged.)

Also update `remember`'s docstring — replace the sentence
`` `body` is the full detail. `` with:

```
`body` is the full detail; link related memories in it with
[[name]] — link liberally, and a [[name]] with no saved memory yet
is fine (it marks a fact worth writing later, not an error).
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_memory.py tests/test_memory_tool.py -v`
Expected: all PASS (including all pre-existing recall tests — bodies without
links must round-trip byte-identically).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests
uv run pyright
git add src/marim_harness/workspace/memory.py src/marim_harness/tools/memory_tools.py \
        tests/test_memory.py tests/test_memory_tool.py
git commit -m "feat(memory): [[wikilink]] convention with link footer in recall"
```

---

### Task 4: guidance polish + full gate

**Files:**
- Modify: `src/marim_harness/tools/memory_tools.py` (`remember`/`recall`
  docstrings only)
- Modify: `src/marim_harness/runtime/instructions.py:91-99` (the two policy
  strings)

**Interfaces:**
- Consumes: nothing new. Text-only — no signatures change.
- Produces: nothing later tasks use (this is the last task).

- [ ] **Step 1: Edit `remember`'s docstring**

Insert two sentences before `Use \`scope="global"\`...`:

```
Use absolute dates, never relative ones ("2026-07-22", not
"today"). Don't save what the repo already records — git history,
AGENTS.md, code structure.
```

- [ ] **Step 2: Edit `recall`'s docstring**

Append as a final sentence (after `always use this.`):

```
Memories reflect when they were written — verify a file, flag, or
function a memory names still exists before acting on it.
```

- [ ] **Step 3: Edit the policy strings in `runtime/instructions.py`**

Replace both constants with:

```python
_PROACTIVE_MEMORY_POLICY = (
    "Proactive memory is ON — save durable user preferences, feedback, and "
    "project conventions with remember. Skip recoverable info, one-off details, "
    "secrets, and anything the repo already records (git history, AGENTS.md, "
    "code structure). Update existing entries over adding duplicates; forget "
    "entries that turn out to be wrong."
)

_ON_REQUEST_MEMORY_POLICY = (
    "Save to memory only when the user explicitly asks (e.g. 'remember that…' "
    "or /remember). Do not save proactively."
)
```

(`_ON_REQUEST_MEMORY_POLICY` is unchanged — shown for context only.)

- [ ] **Step 4: Run the full CI gate in CI order**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: ruff clean, pyright clean, pytest green with coverage ≥90%. If only
the known-flaky `test_fresh_log_top_aligned_then_anchors_on_overflow` fails,
rerun it alone (`uv run pytest --no-cov tests/test_app.py::test_fresh_log_top_aligned_then_anchors_on_overflow`)
and treat a solo pass as green.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/tools/memory_tools.py src/marim_harness/runtime/instructions.py
git commit -m "docs(memory): store-hygiene guidance in tool docstrings and policy"
```
