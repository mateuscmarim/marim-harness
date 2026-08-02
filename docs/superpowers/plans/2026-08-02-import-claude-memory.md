# `marim import claude` (memory slice) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `marim import claude` CLI command that copies a Claude Code CLI memory store into the workspace's `.marim/memory`, dry-run by default.

**Architecture:** One new pure-plus-thin-IO module (`workspace/claude_import.py`) doing path math, frontmatter parsing, and plan building, with `memory.save_memory` as the only writer; one new thin CLI module (`interfaces/cli/import_cmd.py`) wiring argparse to it, registered through `router.py`'s existing `_MODULE_NAMES` seam.

**Tech Stack:** Python 3.10+, `argparse`, `pyyaml` (already a dependency), `pytest`, `ruff`, `pyright`. Managed with `uv`.

**Spec:** `docs/superpowers/specs/2026-08-02-import-claude-memory-design.md`

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never `pip` or a bare `python`/`pytest`.
- `requires-python` is `>=3.10`. No 3.11+ only syntax (no `Self`, no `except*`, no `typing.override`).
- Ruff line length 100. Lint set `E,F,I,UP,B,SIM,C901` — import sorting is enforced.
- Cyclomatic complexity capped at 10 (`C901`). Extract named helpers rather than adding `# noqa: C901`.
- Gate order before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- Keep pure decision/parse helpers side-effect-free and unit-tested directly; effectful I/O stays in its own named functions (`read_source`, `apply_plan`). This mirrors the `tools/impl/` vs `tools/*_tools.py` split described in `CLAUDE.md`.
- Long explanatory comments on *why* a non-obvious invariant holds are the house style. Match it.
- Every new public function gets a docstring; tool-style terseness is not the norm here.
- Branch is `feat/import-claude-memory`, already created off `master`. Commit after every task.

---

## File Structure

**Create:**
- `src/marim_harness/workspace/claude_import.py` — the whole import engine: path derivation, frontmatter parsing, conflict planning, and the single `apply_plan` writer. One responsibility: turning a Claude memory dir into marim memory writes.
- `src/marim_harness/interfaces/cli/import_cmd.py` — argparse surface, source resolution, output rendering, exit codes. Thin wiring only; no format knowledge.
- `tests/test_claude_import.py` — unit tests for the engine.
- `tests/test_cli_import.py` — CLI-level tests, in the style of `tests/test_cli_trust.py`.

**Modify:**
- `src/marim_harness/workspace/memory.py:103` — rename `_index_entries` → `index_entries` (public); update its one caller at `:132`.
- `src/marim_harness/interfaces/cli/router.py:13,20` — add `"import"` to `_MANAGEMENT` and `{"import": "import_cmd"}` to `_MODULE_NAMES`.
- `docs/guides/skills-and-memory.md` — new "Importing from Claude Code" section.
- `ROADMAP.md:30-35` — mark the memory slice landed.
- `CHANGELOG.md` — Unreleased entry.

---

## Spec refinement adopted in this plan

The spec's conflict rule was "skip when `<target>/<slug>.md` exists". That is
necessary but **not sufficient**, and Task 3 implements the stronger rule:

`memory._allocate_slug` reuses an existing entry's slug when the *title*
matches. So importing a Claude memory titled `"Foo"` into a target that already
has `"Foo"` stored under a **different** slug would make `save_memory` write
into that other slug's file — clobbering it even though the source slug's file
does not exist. Conflict detection therefore checks both the slug and the title.

---

### Task 1: Claude path derivation

**Files:**
- Create: `src/marim_harness/workspace/claude_import.py`
- Test: `tests/test_claude_import.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `claude_config_dir(env: Mapping[str, str] | None = None) -> Path`
  - `claude_project_slug(path: Path | str) -> str`
  - `claude_memory_dir(workspace: Path | str, *, config_dir: Path) -> Path`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_claude_import.py`:

```python
from pathlib import Path

from marim_harness.workspace import claude_import


def test_project_slug_replaces_slashes_and_dots():
    """Claude names its project dirs by replacing every `/` and `.` with `-`.
    The leading `-` falls out of the leading `/`. Paths here are absolute and
    non-existent, so the internal resolve() is an identity transform."""
    slug = claude_import.claude_project_slug("/home/x/Projects/marim.dev/marim-harness")
    assert slug == "-home-x-Projects-marim-dev-marim-harness"


def test_project_slug_doubles_dash_for_dot_directories():
    """`/.local` contributes both the separator dash and the dot dash, which is
    why real Claude dirs read `-home-x--local-share-...`."""
    slug = claude_import.claude_project_slug("/home/x/.local/share/fcstudio")
    assert slug == "-home-x--local-share-fcstudio"


def test_project_slug_normalizes_trailing_slash_and_dot_segments():
    assert claude_import.claude_project_slug("/home/x/proj/") == "-home-x-proj"
    assert claude_import.claude_project_slug("/home/x/a/../proj") == "-home-x-proj"


def test_config_dir_honors_env_override(tmp_path: Path):
    got = claude_import.claude_config_dir({"CLAUDE_CONFIG_DIR": str(tmp_path / "cc")})
    assert got == tmp_path / "cc"


def test_config_dir_ignores_blank_override_and_falls_back_to_home():
    got = claude_import.claude_config_dir({"CLAUDE_CONFIG_DIR": "   "})
    assert got == Path.home() / ".claude"


def test_config_dir_defaults_to_dot_claude_in_home():
    assert claude_import.claude_config_dir({}) == Path.home() / ".claude"


def test_memory_dir_composes_projects_slug_memory(tmp_path: Path):
    got = claude_import.claude_memory_dir(
        "/home/x/Projects/app", config_dir=tmp_path / "cc"
    )
    assert got == tmp_path / "cc" / "projects" / "-home-x-Projects-app" / "memory"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_claude_import.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.workspace.claude_import'`

- [ ] **Step 3: Write the module**

Create `src/marim_harness/workspace/claude_import.py`:

```python
"""Import a Claude Code CLI memory store into marim's memory.

Claude Code keeps memory per *project directory*, outside the repo, under
``<claude-config>/projects/<cwd-slug>/memory/``. The on-disk shape there is the
one :mod:`marim_harness.workspace.memory` deliberately mirrors — a ``MEMORY.md``
index of one-line pointers plus one ``<slug>.md`` per fact — so this module is a
format *bridge*, not a translation.

The split follows the house convention: everything above ``read_source`` is
pure (path math, frontmatter parsing, conflict planning) and unit-tested
directly; ``read_source`` and ``apply_plan`` are the only functions that touch
disk, and ``apply_plan`` delegates every write to ``memory.save_memory`` so the
memory format keeps exactly one writer.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path

_DEFAULT_CLAUDE_DIRNAME = ".claude"

# Claude's project-dir naming: every path separator and every dot becomes a
# dash. Both characters share one rule, which is why `/home/x/.local` yields the
# doubled `-home-x--local` seen on disk (one dash for the `/`, one for the `.`).
_SLUG_CHARS_RE = re.compile(r"[/.]")


def claude_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Claude Code's config root: ``$CLAUDE_CONFIG_DIR`` when set to a non-blank
    value, else ``~/.claude``. ``env`` defaults to the live environment and is
    injectable so the pure path helpers stay testable without monkeypatching."""
    env = os.environ if env is None else env
    raw = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / _DEFAULT_CLAUDE_DIRNAME


def claude_project_slug(path: Path | str) -> str:
    """The directory name Claude Code uses for ``path``'s project.

    The path is resolved first (absolute, ``..`` collapsed, symlinks followed)
    so a relative or messy workspace argument lands on the same slug the Claude
    CLI would have produced from its own cwd.
    """
    resolved = Path(path).expanduser().resolve()
    return _SLUG_CHARS_RE.sub("-", str(resolved))


def claude_memory_dir(workspace: Path | str, *, config_dir: Path) -> Path:
    """Where Claude Code keeps the memory store for ``workspace``. The directory
    is not guaranteed to exist — callers check and fall back to listing."""
    return Path(config_dir) / "projects" / claude_project_slug(workspace) / "memory"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_claude_import.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src tests && uv run pyright`
Expected: both clean

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/workspace/claude_import.py tests/test_claude_import.py
git commit -m "feat(import): derive Claude Code's per-project memory dir"
```

---

### Task 2: Parse Claude memory files and recover titles

**Files:**
- Modify: `src/marim_harness/workspace/memory.py:103` (rename `_index_entries` → `index_entries`), `:132` (its one caller)
- Modify: `src/marim_harness/workspace/claude_import.py`
- Test: `tests/test_claude_import.py`

**Interfaces:**
- Consumes: `claude_import.claude_memory_dir` (Task 1); `memory.MemoryScope`.
- Produces:
  - `memory.index_entries(scope: MemoryScope) -> list[tuple[str, str]]` — public rename, returns `(title, slug)` in file order.
  - `ImportedMemory` frozen dataclass with fields `slug: str`, `title: str`, `description: str`, `mem_type: str`, `body: str`.
  - `parse_memory_file(text: str, *, slug: str, title: str) -> ImportedMemory | None`
  - `SourceScan` frozen dataclass with fields `memories: tuple[ImportedMemory, ...]`, `problems: tuple[str, ...]`.
  - `read_source(memory_dir: Path) -> SourceScan`

**Background the implementer needs:** a Claude memory file carries `name`,
`description` and `metadata.type` in YAML frontmatter, but **no title**. The
title lives only in Claude's `MEMORY.md`, in the same `- [Title](slug.md) — hook`
line shape marim writes — hence the `index_entries` promotion instead of a
second parser. The file's `name:` key is ignored: the filename stem is the
authoritative slug (it is what the index links to, and `save_memory` re-renders
`name:` from its own allocated slug anyway).

- [ ] **Step 1: Rename `_index_entries` to `index_entries`**

In `src/marim_harness/workspace/memory.py`, rename the definition at line 103 and
its single caller inside `_allocate_slug` at line 132. Do not leave a private
alias — one caller, one name. Extend the docstring to record the second reader:

```python
def index_entries(scope: MemoryScope) -> list[tuple[str, str]]:
    """``(title, slug)`` for every entry in ``MEMORY.md``, in file order.
    Best-effort: an absent/unreadable index yields ``[]`` (never raises).

    Public because the Claude importer (``workspace.claude_import``) reads a
    *foreign* store's index through the same parser — Claude keeps a memory's
    title only in its index line, and sharing this one regex is what keeps the
    two readers from drifting."""
```

- [ ] **Step 2: Run the memory tests to verify the rename is clean**

Run: `uv run pytest --no-cov -n 0 tests/test_memory.py -v`
Expected: PASS. If a test referenced `memory._index_entries` by name, update it to `memory.index_entries`.

- [ ] **Step 3: Write the failing parse tests**

Append to `tests/test_claude_import.py`:

```python
_FILE = """---
name: deploy-notes
description: How the deploy works.
metadata:
  node_type: memory
  type: project
  originSessionId: abc-123
  modified: 2026-07-28T18:12:01.714Z
---

Body line one.

A separator inside the body:

---

Body after the separator.
"""


def test_parse_memory_file_extracts_fields_and_keeps_body():
    got = claude_import.parse_memory_file(_FILE, slug="deploy-notes", title="Deploy notes")
    assert got is not None
    assert got.slug == "deploy-notes"
    assert got.title == "Deploy notes"
    assert got.description == "How the deploy works."
    assert got.mem_type == "project"
    assert got.body.startswith("Body line one.")
    assert "Body after the separator." in got.body


def test_parse_memory_file_tolerates_missing_description_and_type():
    text = "---\nname: x\n---\n\nBody.\n"
    got = claude_import.parse_memory_file(text, slug="x", title="X")
    assert got is not None
    assert got.description == ""
    assert got.mem_type == "project"


def test_parse_memory_file_rejects_text_without_frontmatter():
    assert claude_import.parse_memory_file("Just a note.\n", slug="x", title="X") is None


def test_parse_memory_file_rejects_non_mapping_frontmatter():
    assert claude_import.parse_memory_file("---\n- a\n- b\n---\nBody\n", slug="x", title="X") is None


def _write_source(root, name, description, body, mem_type="project"):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"metadata:\n  type: {mem_type}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_read_source_recovers_titles_from_the_index(tmp_path: Path):
    src = tmp_path / "memory"
    _write_source(src, "alpha", "First fact.", "Alpha body.")
    _write_source(src, "beta", "Second fact.", "Beta body.")
    (src / "MEMORY.md").write_text(
        "# Memory Index\n\n"
        "- [Alpha Fact](alpha.md) — first\n"
        "- [Beta Fact](beta.md) — second\n",
        encoding="utf-8",
    )
    scan = claude_import.read_source(src)
    assert [(m.slug, m.title) for m in scan.memories] == [
        ("alpha", "Alpha Fact"),
        ("beta", "Beta Fact"),
    ]
    assert scan.problems == ()


def test_read_source_does_not_misattribute_a_title_containing_a_link(tmp_path: Path):
    """`index_entries` anchors on the FIRST `](...md)` of a line. An entry whose
    hook text mentions another memory's file must still resolve to its own slug,
    or the importer would hand `beta`'s title to `alpha`."""
    src = tmp_path / "memory"
    _write_source(src, "alpha", "First fact.", "Alpha body.")
    _write_source(src, "beta", "Second fact.", "Beta body.")
    (src / "MEMORY.md").write_text(
        "- [Alpha Fact](alpha.md) — see also [Beta](beta.md)\n"
        "- [Beta Fact](beta.md) — second\n",
        encoding="utf-8",
    )
    scan = claude_import.read_source(src)
    assert [(m.slug, m.title) for m in scan.memories] == [
        ("alpha", "Alpha Fact"),
        ("beta", "Beta Fact"),
    ]


def test_read_source_falls_back_to_the_slug_for_orphan_files(tmp_path: Path):
    """A memory file with no index entry still imports; its slug becomes the
    title, which is what a hand-written memory would have looked like anyway."""
    src = tmp_path / "memory"
    _write_source(src, "orphan", "No index line.", "Orphan body.")
    scan = claude_import.read_source(src)
    assert [(m.slug, m.title) for m in scan.memories] == [("orphan", "orphan")]


def test_read_source_skips_the_index_itself_and_reports_unparseable(tmp_path: Path):
    src = tmp_path / "memory"
    _write_source(src, "good", "Fine.", "Good body.")
    (src / "junk.md").write_text("no frontmatter here\n", encoding="utf-8")
    (src / "MEMORY.md").write_text("- [Good](good.md) — hook\n", encoding="utf-8")
    scan = claude_import.read_source(src)
    assert [m.slug for m in scan.memories] == ["good"]
    assert any("junk.md" in p for p in scan.problems)


def test_read_source_reports_undecodable_file(tmp_path: Path):
    src = tmp_path / "memory"
    _write_source(src, "good", "Fine.", "Good body.")
    (src / "binary.md").write_bytes(b"\xff\xfe\x00bad")
    scan = claude_import.read_source(src)
    assert [m.slug for m in scan.memories] == ["good"]
    assert any("binary.md" in p for p in scan.problems)


def test_read_source_on_empty_dir_is_empty(tmp_path: Path):
    src = tmp_path / "memory"
    src.mkdir()
    scan = claude_import.read_source(src)
    assert scan.memories == () and scan.problems == ()
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_claude_import.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'parse_memory_file'`

- [ ] **Step 5: Implement parsing and source reading**

Add to the imports at the top of `src/marim_harness/workspace/claude_import.py`:

```python
from dataclasses import dataclass

import yaml

from ._frontmatter import FRONTMATTER_RE
from .memory import MemoryScope, index_entries

_INDEX_FILE = "MEMORY.md"
_DEFAULT_TYPE = "project"
```

(No module logger: nothing here logs. Failures are returned as data — `None`
from `parse_memory_file`, `problems` from `read_source` — because this module's
consumer is a CLI that prints them, not a turn that must stay quiet.)

Append to the module:

```python
@dataclass(frozen=True)
class ImportedMemory:
    """One Claude memory file, parsed into exactly the arguments
    ``memory.save_memory`` takes."""

    slug: str
    title: str
    description: str
    mem_type: str
    body: str


@dataclass(frozen=True)
class SourceScan:
    """What one pass over a Claude memory dir found: the memories worth
    importing, plus a human-readable line per file that could not be read or
    parsed. Problems are reported, never fatal — one corrupt file must not cost
    the user the rest of their store."""

    memories: tuple[ImportedMemory, ...]
    problems: tuple[str, ...]


def parse_memory_file(text: str, *, slug: str, title: str) -> ImportedMemory | None:
    """Parse one Claude memory file. Returns ``None`` when the text has no
    parseable YAML mapping frontmatter — marim's format always writes one, so a
    file without it is not a memory (a stray note, a partial write) and is
    skipped rather than imported with empty metadata.

    ``slug`` comes from the filename and ``title`` from the source index; the
    file's own ``name:`` key is deliberately ignored, since the filename is what
    the index links to and ``save_memory`` re-renders ``name:`` regardless.
    Claude's extra keys (``node_type``, ``originSessionId``, ``modified``) are
    dropped: marim reads none of them, and passing them through would fork the
    format the two tools currently share.
    """
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    description = data.get("description")
    metadata = data.get("metadata")
    mem_type = metadata.get("type") if isinstance(metadata, dict) else None
    return ImportedMemory(
        slug=slug,
        title=title,
        description=str(description).strip() if isinstance(description, str) else "",
        # save_memory's _render_frontmatter coerces an unrecognized type to
        # "project" anyway; defaulting here too keeps the parsed value honest
        # about what will be written.
        mem_type=mem_type if isinstance(mem_type, str) else _DEFAULT_TYPE,
        body=match.group(2),
    )


def read_source(memory_dir: Path) -> SourceScan:
    """Every parseable memory in a Claude memory dir, sorted by slug.

    Titles come from the dir's own ``MEMORY.md``, read through marim's index
    parser; a file with no index entry falls back to its slug as the title.
    """
    titles = {slug: title for title, slug in index_entries(MemoryScope("claude", memory_dir))}
    memories: list[ImportedMemory] = []
    problems: list[str] = []
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == _INDEX_FILE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{path.name}: unreadable ({exc.__class__.__name__})")
            continue
        parsed = parse_memory_file(text, slug=path.stem, title=titles.get(path.stem, path.stem))
        if parsed is None:
            problems.append(f"{path.name}: no usable frontmatter — skipped")
            continue
        memories.append(parsed)
    return SourceScan(memories=tuple(memories), problems=tuple(problems))
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_claude_import.py tests/test_memory.py -v`
Expected: PASS

- [ ] **Step 7: Lint and type-check**

Run: `uv run ruff check src tests && uv run pyright`
Expected: both clean

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/workspace/claude_import.py src/marim_harness/workspace/memory.py tests/test_claude_import.py
git commit -m "feat(import): parse Claude memory files and recover titles from its index"
```

---

### Task 3: Conflict planning

**Files:**
- Modify: `src/marim_harness/workspace/claude_import.py`
- Test: `tests/test_claude_import.py`

**Interfaces:**
- Consumes: `ImportedMemory` (Task 2).
- Produces:
  - `PlannedImport` frozen dataclass with fields `action: str` (`"import"` | `"overwrite"` | `"skip"`), `slug: str`, `title: str`, `reason: str` (empty unless skipped).
  - `plan_import(sources: Sequence[ImportedMemory], *, existing_slugs: set[str], existing_titles: dict[str, str], force: bool) -> list[PlannedImport]`
  - `target_state(scope: MemoryScope) -> tuple[set[str], dict[str, str]]` — reads the target dir, returning its existing slugs and its `title -> slug` map.

**Why both checks (this is the spec refinement):** `memory._allocate_slug`
reuses an existing entry's slug on a **title** match. So a source titled `"Foo"`
whose slug is free can still land on top of a target memory stored under a
*different* slug that already claims the title `"Foo"`. Slug-existence alone
would call that a clean import and silently clobber. Detect both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claude_import.py`:

```python
def _mem(slug, title):
    return claude_import.ImportedMemory(
        slug=slug, title=title, description="d", mem_type="project", body="b"
    )


def test_plan_import_marks_fresh_memories_as_import():
    plan = claude_import.plan_import(
        [_mem("alpha", "Alpha")], existing_slugs=set(), existing_titles={}, force=False
    )
    assert [(p.action, p.slug) for p in plan] == [("import", "alpha")]
    assert plan[0].reason == ""


def test_plan_import_skips_an_existing_slug():
    plan = claude_import.plan_import(
        [_mem("alpha", "Alpha")],
        existing_slugs={"alpha"},
        existing_titles={},
        force=False,
    )
    assert plan[0].action == "skip"
    assert "already present" in plan[0].reason


def test_plan_import_skips_a_title_claimed_by_a_different_slug():
    """The clobber _allocate_slug would otherwise cause: the source slug is
    free, but the target already stores that title under another slug, so
    save_memory would write into *that* file."""
    plan = claude_import.plan_import(
        [_mem("alpha", "Shared Title")],
        existing_slugs={"other"},
        existing_titles={"Shared Title": "other"},
        force=False,
    )
    assert plan[0].action == "skip"
    assert "other" in plan[0].reason


def test_plan_import_allows_a_title_already_owned_by_the_same_slug():
    """Re-importing the same memory is a refresh, not a cross-slug clobber, so
    it is an ordinary overwrite decision rather than a title conflict."""
    plan = claude_import.plan_import(
        [_mem("alpha", "Alpha")],
        existing_slugs=set(),
        existing_titles={"Alpha": "alpha"},
        force=False,
    )
    assert plan[0].action == "import"


def test_plan_import_force_turns_conflicts_into_overwrites():
    plan = claude_import.plan_import(
        [_mem("alpha", "Alpha"), _mem("beta", "Beta")],
        existing_slugs={"alpha"},
        existing_titles={"Beta": "gamma"},
        force=True,
    )
    assert [(p.action, p.slug) for p in plan] == [("overwrite", "alpha"), ("overwrite", "beta")]


def test_plan_import_preserves_source_order():
    plan = claude_import.plan_import(
        [_mem("c", "C"), _mem("a", "A")],
        existing_slugs=set(),
        existing_titles={},
        force=False,
    )
    assert [p.slug for p in plan] == ["c", "a"]


def test_target_state_reads_slugs_and_titles(tmp_path: Path):
    from marim_harness.workspace import memory

    scope = memory.project_scope(tmp_path)
    memory.save_memory(
        scope, name="Alpha", description="d", mem_type="project", body="b", title="Alpha"
    )
    slugs, titles = claude_import.target_state(scope)
    assert slugs == {"alpha"}
    assert titles == {"Alpha": "alpha"}


def test_target_state_on_missing_dir_is_empty(tmp_path: Path):
    from marim_harness.workspace import memory

    slugs, titles = claude_import.target_state(memory.project_scope(tmp_path))
    assert slugs == set() and titles == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_claude_import.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'plan_import'`

- [ ] **Step 3: Implement planning**

Add `from collections.abc import Mapping, Sequence` (replacing the existing
`Mapping`-only import) at the top of `claude_import.py`, then append:

```python
@dataclass(frozen=True)
class PlannedImport:
    """What the importer decided to do about one source memory. ``reason`` is
    empty except on a skip, where it explains the conflict well enough for the
    user to decide whether ``--force`` is what they want."""

    action: str  # "import" | "overwrite" | "skip"
    slug: str
    title: str
    reason: str = ""


def target_state(scope: MemoryScope) -> tuple[set[str], dict[str, str]]:
    """The target scope's existing ``<slug>.md`` files and its ``title -> slug``
    map. Slugs come from the *files*, not the index, because the index can be
    stale — the same reasoning as ``memory._link_saved``. Titles necessarily
    come from the index, which is where ``_allocate_slug`` reads them."""
    try:
        slugs = {p.stem for p in scope.root.glob("*.md") if p.name != _INDEX_FILE}
    except OSError:
        slugs = set()
    titles = {title: slug for title, slug in index_entries(scope)}
    return slugs, titles


def _conflict(memory_: ImportedMemory, existing_slugs, existing_titles) -> str:
    """Why importing ``memory_`` would destroy something, or ``""`` if it would
    not. Two independent hazards, both real:

    1. The slug's file already exists — the obvious collision.
    2. The title is already claimed by a *different* slug. ``save_memory`` routes
       through ``_allocate_slug``, which reuses an existing entry's slug on a
       title match, so this would write into the other memory's file even though
       the source slug is free. A same-slug title match is not a conflict; that
       is just a refresh of the same memory.
    """
    if memory_.slug in existing_slugs:
        return "already present — use --force"
    owner = existing_titles.get(memory_.title)
    if owner is not None and owner != memory_.slug:
        return f"title already used by {owner!r} — use --force"
    return ""


def plan_import(
    sources: Sequence[ImportedMemory],
    *,
    existing_slugs: set[str],
    existing_titles: dict[str, str],
    force: bool,
) -> list[PlannedImport]:
    """Decide import / overwrite / skip for each source, in source order.
    Pure: takes the target's state as data so it can be tested without a disk."""
    planned: list[PlannedImport] = []
    for source in sources:
        reason = _conflict(source, existing_slugs, existing_titles)
        if not reason:
            action, reason = "import", ""
        elif force:
            action, reason = "overwrite", ""
        else:
            action = "skip"
        planned.append(
            PlannedImport(action=action, slug=source.slug, title=source.title, reason=reason)
        )
    return planned
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_claude_import.py -v`
Expected: PASS

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src tests && uv run pyright`
Expected: both clean

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/workspace/claude_import.py tests/test_claude_import.py
git commit -m "feat(import): plan Claude memory imports, guarding slug and title clobbers"
```

---

### Task 4: Apply the plan

**Files:**
- Modify: `src/marim_harness/workspace/claude_import.py`
- Test: `tests/test_claude_import.py`

**Interfaces:**
- Consumes: `ImportedMemory`, `PlannedImport` (Tasks 2-3); `memory.save_memory`.
- Produces:
  - `ImportResult` frozen dataclass with fields `imported: tuple[str, ...]`, `skipped: tuple[str, ...]`, `failed: tuple[str, ...]` (each a tuple of slugs).
  - `apply_plan(plan: Sequence[PlannedImport], sources: Sequence[ImportedMemory], scope: MemoryScope) -> ImportResult`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claude_import.py`:

```python
def test_apply_plan_writes_files_and_index(tmp_path: Path):
    from marim_harness.workspace import memory

    scope = memory.project_scope(tmp_path)
    sources = [_mem("alpha", "Alpha Fact"), _mem("beta", "Beta Fact")]
    plan = claude_import.plan_import(
        sources, existing_slugs=set(), existing_titles={}, force=False
    )
    result = claude_import.apply_plan(plan, sources, scope)

    assert result.imported == ("alpha", "beta")
    assert result.skipped == () and result.failed == ()
    written = (scope.root / "alpha.md").read_text(encoding="utf-8")
    assert "name: alpha" in written
    assert "type: project" in written
    index = (scope.root / "MEMORY.md").read_text(encoding="utf-8")
    assert "- [Alpha Fact](alpha.md)" in index
    assert "- [Beta Fact](beta.md)" in index


def test_apply_plan_does_not_write_skipped_memories(tmp_path: Path):
    from marim_harness.workspace import memory

    scope = memory.project_scope(tmp_path)
    memory.save_memory(
        scope, name="alpha", description="mine", mem_type="project", body="marim body",
        title="Alpha Fact",
    )
    sources = [_mem("alpha", "Alpha Fact")]
    plan = claude_import.plan_import(
        sources, existing_slugs={"alpha"}, existing_titles={"Alpha Fact": "alpha"}, force=False
    )
    result = claude_import.apply_plan(plan, sources, scope)

    assert result.imported == () and result.skipped == ("alpha",)
    assert "marim body" in (scope.root / "alpha.md").read_text(encoding="utf-8")


def test_apply_plan_overwrites_under_force(tmp_path: Path):
    from marim_harness.workspace import memory

    scope = memory.project_scope(tmp_path)
    memory.save_memory(
        scope, name="alpha", description="mine", mem_type="project", body="marim body",
        title="Alpha Fact",
    )
    sources = [_mem("alpha", "Alpha Fact")]
    plan = claude_import.plan_import(
        sources, existing_slugs={"alpha"}, existing_titles={"Alpha Fact": "alpha"}, force=True
    )
    result = claude_import.apply_plan(plan, sources, scope)

    assert result.imported == ("alpha",)
    assert (scope.root / "alpha.md").read_text(encoding="utf-8").rstrip().endswith("b")


def test_apply_plan_records_a_failed_write(tmp_path: Path, monkeypatch):
    """save_memory returns None instead of raising when the write fails; the
    importer must surface that as a failure rather than count it as imported."""
    from marim_harness.workspace import memory

    monkeypatch.setattr(claude_import, "save_memory", lambda *a, **k: None)
    scope = memory.project_scope(tmp_path)
    sources = [_mem("alpha", "Alpha")]
    plan = claude_import.plan_import(
        sources, existing_slugs=set(), existing_titles={}, force=False
    )
    result = claude_import.apply_plan(plan, sources, scope)

    assert result.failed == ("alpha",) and result.imported == ()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_claude_import.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'apply_plan'`

- [ ] **Step 3: Implement the writer**

Change the `memory` import at the top of `claude_import.py` to also pull in the
writer — note it is imported *by name* so the `monkeypatch.setattr` in the test
above can swap it:

```python
from .memory import MemoryScope, index_entries, save_memory
```

Append to the module:

```python
@dataclass(frozen=True)
class ImportResult:
    """The outcome of one ``apply_plan``, as three slug tuples. A non-empty
    ``failed`` is what makes the CLI exit non-zero."""

    imported: tuple[str, ...]
    skipped: tuple[str, ...]
    failed: tuple[str, ...]


def apply_plan(
    plan: Sequence[PlannedImport],
    sources: Sequence[ImportedMemory],
    scope: MemoryScope,
) -> ImportResult:
    """Perform every non-skipped entry of ``plan``, writing through
    ``memory.save_memory`` so the memory format keeps a single writer — index
    upsert, slug allocation, atomic writes and the advisory lock all come from
    there rather than being reimplemented here.

    ``save_memory`` never raises (it logs and returns ``None`` on a failed
    write, per its fail-soft contract for tool calls), so a falsy return is the
    only failure signal there is; it is collected rather than swallowed because
    this runs in a CLI, where failures should be loud.
    """
    by_slug = {source.slug: source for source in sources}
    imported: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for entry in plan:
        if entry.action == "skip":
            skipped.append(entry.slug)
            continue
        source = by_slug.get(entry.slug)
        if source is None:  # pragma: no cover - plan is always built from sources
            failed.append(entry.slug)
            continue
        written = save_memory(
            scope,
            name=source.slug,
            description=source.description,
            mem_type=source.mem_type,
            body=source.body,
            title=source.title,
        )
        (imported if written is not None else failed).append(entry.slug)
    return ImportResult(
        imported=tuple(imported), skipped=tuple(skipped), failed=tuple(failed)
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_claude_import.py -v`
Expected: PASS

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src tests && uv run pyright`
Expected: both clean

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/workspace/claude_import.py tests/test_claude_import.py
git commit -m "feat(import): apply an import plan through save_memory"
```

---

### Task 5: The `marim import claude` command

**Files:**
- Create: `src/marim_harness/interfaces/cli/import_cmd.py`
- Modify: `src/marim_harness/interfaces/cli/router.py:13` and `:20`
- Test: `tests/test_cli_import.py`

**Interfaces:**
- Consumes: everything from Tasks 1-4 — `claude_config_dir`, `claude_memory_dir`, `read_source`, `target_state`, `plan_import`, `apply_plan`, `SourceScan`, `PlannedImport`, `ImportResult`; `memory.project_scope`.
- Produces: `main(argv: list[str], *, out=None, err=None) -> int` and the alias `run = main`.

**Exit codes:** `0` success (dry run or apply, nothing failed); `1` source dir not found, or any `save_memory` failure; `2` a bad argument (workspace or `--from` is not a directory), matching `trust_cmd`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_import.py`:

```python
from pathlib import Path


def _claude_store(config_dir: Path, workspace: Path, memories):
    """Build a fake Claude memory store for `workspace` under `config_dir`."""
    from marim_harness.workspace.claude_import import claude_memory_dir

    src = claude_memory_dir(workspace, config_dir=config_dir)
    src.mkdir(parents=True)
    lines = []
    for slug, title, body in memories:
        (src / f"{slug}.md").write_text(
            f"---\nname: {slug}\ndescription: about {slug}\n"
            f"metadata:\n  type: project\n---\n\n{body}\n",
            encoding="utf-8",
        )
        lines.append(f"- [{title}]({slug}.md) — hook for {slug}")
    (src / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return src


def test_dry_run_lists_but_writes_nothing(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws)]) == 0
    out = capsys.readouterr().out
    assert "import" in out and "alpha" in out
    assert "Dry run" in out
    assert not (ws / ".marim" / "memory").exists()


def test_apply_writes_the_memories(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--apply"]) == 0
    target = ws / ".marim" / "memory"
    assert "A body" in (target / "alpha.md").read_text(encoding="utf-8")
    assert "- [Alpha Fact](alpha.md)" in (target / "MEMORY.md").read_text(encoding="utf-8")
    assert "Dry run" not in capsys.readouterr().out


def test_second_apply_skips_without_force(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    run(["claude", str(ws), "--apply"])
    capsys.readouterr()
    assert run(["claude", str(ws), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "skip" in out
    assert "1 skipped" in out


def test_force_overwrites(tmp_path, capsys, monkeypatch):
    """Import once, rewrite the source memory's body, import again with
    --force: the new body must win."""
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "original")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run
    from marim_harness.workspace.claude_import import claude_memory_dir

    run(["claude", str(ws), "--apply"])
    (claude_memory_dir(ws, config_dir=cfg) / "alpha.md").write_text(
        "---\nname: alpha\ndescription: about alpha\n"
        "metadata:\n  type: project\n---\n\nrewritten\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert run(["claude", str(ws), "--apply", "--force"]) == 0
    body = (ws / ".marim" / "memory" / "alpha.md").read_text(encoding="utf-8")
    assert "rewritten" in body and "original" not in body


def test_missing_source_lists_candidates_and_exits_one(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, other, [("beta", "Beta", "B body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws)]) == 1
    err = capsys.readouterr().err
    assert "--from" in err
    assert "other" in err


def test_from_accepts_a_project_dir_or_a_memory_dir(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    other = tmp_path / "other"
    other.mkdir()
    src = _claude_store(cfg, other, [("beta", "Beta", "B body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--from", str(src)]) == 0
    assert "beta" in capsys.readouterr().out
    assert run(["claude", str(ws), "--from", str(src.parent)]) == 0
    assert "beta" in capsys.readouterr().out


def test_bad_workspace_exits_two(tmp_path, capsys):
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(tmp_path / "nope")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_failed_write_exits_one(tmp_path, capsys, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "cc"
    _claude_store(cfg, ws, [("alpha", "Alpha Fact", "A body")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    from marim_harness.workspace import claude_import

    monkeypatch.setattr(claude_import, "save_memory", lambda *a, **k: None)
    from marim_harness.interfaces.cli.import_cmd import run

    assert run(["claude", str(ws), "--apply"]) == 1
    assert "alpha" in capsys.readouterr().err


def test_router_routes_the_import_keyword():
    from marim_harness.interfaces.cli import router

    assert "import" in router._MANAGEMENT
    assert router._MODULE_NAMES["import"] == "import_cmd"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_cli_import.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.interfaces.cli.import_cmd'`

- [ ] **Step 3: Write the CLI module**

Create `src/marim_harness/interfaces/cli/import_cmd.py`:

```python
"""`marim import claude` — carry an existing Claude Code CLI setup into marim.

Today this imports exactly one thing: the *memory store*. marim's memory format
deliberately mirrors Claude Code's, so an importer is how that promise stays
checkable — format drift shows up here as an import gap rather than as a
surprise on switching day. Skills, sub-agents, hooks and MCP servers are
separate slices with their own trust questions and are not handled here.

The module is thin wiring: every format decision lives in
``workspace.claude_import``.
"""

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from ...workspace.claude_import import (
    PlannedImport,
    SourceScan,
    apply_plan,
    claude_config_dir,
    claude_memory_dir,
    plan_import,
    read_source,
    target_state,
)
from ...workspace.memory import project_scope


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marim import",
        description="Import an existing Claude Code setup into this workspace.",
    )
    parser.add_argument(
        "source", choices=["claude"], help="What to import from. Only `claude` today."
    )
    parser.add_argument("workspace", nargs="?", default=".", help="Project root (default: cwd).")
    parser.add_argument(
        "--from", dest="from_dir", default=None,
        help="Claude memory dir (or the project dir containing it), skipping auto-detection.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Perform the import. Without this the command only reports what it would do.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite marim memories that conflict with an imported one.",
    )
    return parser


def _candidate_dirs(config_dir: Path) -> list[tuple[Path, int]]:
    """Every Claude project memory dir that exists, with its memory count, so a
    failed auto-detection can tell the user what `--from` could point at."""
    projects = config_dir / "projects"
    found: list[tuple[Path, int]] = []
    if not projects.is_dir():
        return found
    for entry in sorted(projects.iterdir()):
        memdir = entry / "memory"
        if memdir.is_dir():
            count = len([p for p in memdir.glob("*.md") if p.name != "MEMORY.md"])
            found.append((memdir, count))
    return found


def _resolve_source(from_dir: str | None, root: Path, *, err) -> Path | None:
    """The Claude memory dir to read, or ``None`` after printing why not.

    ``--from`` accepts either the memory dir itself or the project dir holding
    it, since the two are easy to confuse when copying a path off a listing.
    """
    if from_dir is not None:
        given = Path(from_dir).expanduser().resolve()
        candidate = given / "memory" if (given / "memory").is_dir() else given
        if not candidate.is_dir():
            print(f"not a directory: {given}", file=err)
            return None
        return candidate
    memdir = claude_memory_dir(root, config_dir=claude_config_dir())
    if memdir.is_dir():
        return memdir
    print(f"no Claude Code memory found for {root}", file=err)
    print(f"  looked in: {memdir}", file=err)
    candidates = _candidate_dirs(claude_config_dir())
    if candidates:
        print("  available stores — re-run with --from <path>:", file=err)
        for path, count in candidates:
            print(f"    {path}  ({count} memories)", file=err)
    return None


def _repo_tracks_target(root: Path) -> bool:
    """Whether writing into ``<root>/.marim`` would land in git-tracked space.

    Only a *definitive* "not ignored" counts. A missing git binary, a
    non-repo, or any unexpected return code means we cannot tell — and a
    warning printed on every run when we cannot tell is worse than silence.
    """
    if not (root / ".git").exists():
        return False
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", ".marim/"],
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 1  # 0 = ignored, 1 = not ignored, other = unknown


def _report_scan(scan: SourceScan, source: Path, target: Path, *, out, err) -> None:
    print(f"source: {source}  ({len(scan.memories)} memories)", file=out)
    print(f"target: {target}", file=out)
    print("", file=out)
    for problem in scan.problems:
        print(f"  source problem — {problem}", file=err)


def _report_plan(plan: Sequence[PlannedImport], *, out) -> None:
    width = max((len(entry.slug) for entry in plan), default=0)
    for entry in plan:
        detail = entry.reason or entry.title
        print(f"  {entry.action:<9} {entry.slug:<{width}}  {detail}", file=out)
    if plan:
        print("", file=out)


def main(argv: list[str], *, out=None, err=None) -> int:
    # `out`/`err` resolve to the *current* streams inside the call rather than
    # being bound at def-time, so pytest's capsys sees this module's output no
    # matter which test imported it first. Same reasoning as trust_cmd.main.
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = _build_parser().parse_args(argv)

    root = Path(args.workspace).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=err)
        return 2

    source = _resolve_source(args.from_dir, root, err=err)
    if source is None:
        return 1

    scan = read_source(source)
    scope = project_scope(root)
    _report_scan(scan, source, scope.root, out=out, err=err)
    if not scan.memories:
        print("nothing to import.", file=out)
        return 0

    existing_slugs, existing_titles = target_state(scope)
    plan = plan_import(
        scan.memories,
        existing_slugs=existing_slugs,
        existing_titles=existing_titles,
        force=args.force,
    )
    _report_plan(plan, out=out)

    if not args.apply:
        pending = sum(1 for entry in plan if entry.action != "skip")
        skipped = len(plan) - pending
        print(f"{pending} to import, {skipped} skipped.", file=out)
        print("Dry run — re-run with --apply to write.", file=out)
        return 0

    if _repo_tracks_target(root):
        print(
            f"warning: {scope.root} is inside a git repo and is not gitignored — "
            "imported memories would be committable.",
            file=err,
        )
    result = apply_plan(plan, scan.memories, scope)
    for slug in result.failed:
        print(f"failed to write memory {slug!r}", file=err)
    print(
        f"{len(result.imported)} imported, {len(result.skipped)} skipped"
        + (f", {len(result.failed)} failed" if result.failed else "")
        + ".",
        file=out,
    )
    return 1 if result.failed else 0


# `run` is the spelling this command's tests were written against; `main` is
# what router.py dispatches to and what every other cli/* module exposes. Same
# alias arrangement as trust_cmd.
run = main
```

- [ ] **Step 4: Register the keyword in the router**

In `src/marim_harness/interfaces/cli/router.py`, change line 13 and line 20:

```python
_MANAGEMENT = {"sessions", "config", "models", "plugin", "mcp", "serve", "trust", "import"}
```

```python
# Keyword -> submodule name, for the cases where they differ. ``trust`` maps to
# ``trust_cmd`` so a bare `import trust` anywhere near this package unambiguously
# means the top-level ``marim_harness.trust`` predicate module; ``import`` maps to
# ``import_cmd`` because `import` is a Python keyword and cannot name a module
# that anything imports by statement. Every other keyword maps to a same-named module.
_MODULE_NAMES = {"trust": "trust_cmd", "import": "import_cmd"}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_cli_import.py tests/test_claude_import.py -v`
Expected: PASS

- [ ] **Step 6: Smoke it against the real store**

Run: `uv run marim import claude`
Expected: a dry-run listing against this repo's actual Claude memory dir, writing nothing. Confirm `.marim/memory` still does not exist afterwards (`ls .marim` — the dir may hold other things; `memory/` must be absent).

- [ ] **Step 7: Lint and type-check**

Run: `uv run ruff check src tests && uv run pyright`
Expected: both clean

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/interfaces/cli/import_cmd.py src/marim_harness/interfaces/cli/router.py tests/test_cli_import.py
git commit -m "feat(import): add the marim import claude command"
```

---

### Task 6: Docs, changelog, and the full gate

**Files:**
- Modify: `docs/guides/skills-and-memory.md`
- Modify: `ROADMAP.md:30-35`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the finished command from Task 5.
- Produces: nothing code-facing.

- [ ] **Step 1: Read the guide to match its voice and heading depth**

Run: `sed -n '1,60p' docs/guides/skills-and-memory.md`

- [ ] **Step 2: Add the import section**

Append a section to `docs/guides/skills-and-memory.md` (match the file's existing heading level for a top-level topic):

```markdown
## Importing from Claude Code

marim's memory format mirrors Claude Code's, so an existing store carries over
directly:

    marim import claude              # dry run — reports, writes nothing
    marim import claude --apply      # perform it

Claude keeps memory per project directory, outside the repo, under
`$CLAUDE_CONFIG_DIR/projects/<path-slug>/memory` (default `~/.claude`). The
command derives that path from the workspace root. If it does not exist — a
worktree, or a project you opened from a different path — it lists the stores it
can see; pass one with `--from`:

    marim import claude --from ~/.claude/projects/-home-me-Projects-app

Memories land in **project scope**, `<workspace>/.marim/memory`, matching the
directory Claude keyed them to. If `.marim/` is not gitignored, `--apply` warns
that the imported memories would be committable.

A memory whose slug already exists in the target is skipped, as is one whose
title is already claimed by a different slug — either would overwrite something
marim's own `remember` tool wrote. `--force` overwrites both. Claude's extra
frontmatter keys (`originSessionId`, `modified`) are dropped; marim reads none
of them.

Project instruction files need no import: marim already reads a `CLAUDE.md` in
the workspace root when there is no `AGENTS.md`. Skills, sub-agents, hooks and
MCP servers are not imported yet.
```

- [ ] **Step 3: Update the roadmap entry**

In `ROADMAP.md`, replace the `marim import claude` bullet (lines 30-35) with a
version noting the memory slice has landed and the rest has not:

```markdown
- **`marim import claude`** — one command that finds an existing Claude Code
  setup and carries it over. The **memory store** ships today (`marim import
  claude`, see `docs/guides/skills-and-memory.md`); hooks configuration,
  skills, sub-agents, MCP servers and user-level `CLAUDE.md` are still to do.
  marim's formats deliberately mirror Claude Code's so user investment stays
  portable; an importer makes that promise checkable — any format drift shows
  up as an import gap, not as a surprise on switching day.
```

- [ ] **Step 4: Add the changelog entry**

Under `## [Unreleased]` in `CHANGELOG.md` (create the section following the
file's existing convention if it is absent), add under `### Added`:

```markdown
- `marim import claude` — import a Claude Code CLI memory store into the
  workspace's `.marim/memory`. Dry-run by default (`--apply` to write),
  auto-detects Claude's per-project store or takes `--from`, and skips anything
  that would overwrite an existing marim memory unless `--force` is passed.
```

- [ ] **Step 5: Run the full gate in CI order**

Run each, in order, and confirm each is clean before the next:

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: ruff clean, pyright clean, full suite green. `tests/test_docs_reference.py`
covers docs cross-references — if it fails, the new guide section has a bad link.

If a single leg of the suite fails on a timing assertion, re-run it before
investigating: this repo has known load-sensitive flakes (see
`docs/superpowers/specs/` history and the provider-error dump test).

- [ ] **Step 6: Commit**

```bash
git add docs/guides/skills-and-memory.md ROADMAP.md CHANGELOG.md
git commit -m "docs: document marim import claude and mark the memory slice landed"
```

---

## Done criteria

- `uv run ruff check src tests`, `uv run pyright`, and `uv run pytest` all clean.
- `marim import claude` against a real Claude store prints a plan and writes nothing.
- `marim import claude --apply` writes `<workspace>/.marim/memory/<slug>.md` files plus a correct `MEMORY.md`, and a second run reports every one as skipped.
- `marim import claude --apply --force` overwrites them.
- Nothing outside `.marim/memory` is written.
