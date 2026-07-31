# Silent Data Corruption Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four defects that silently destroy or corrupt user data — `edit_file` rewriting untouched bytes, `forget`/`recall` resolving to the wrong memory file, colliding memory titles overwriting each other, and a workflow result discarded when its spill write fails.

> **Run Task 1 of [`2026-07-30-tui-consent-and-crash-fixes.md`](2026-07-30-tui-consent-and-crash-fixes.md)
> before this plan.** That task closes T-0, the approval-preview spoofing bug, which defeats
> the control every other gate depends on. Everything in *this* plan is independent of it and
> touches disjoint files, so the rest can proceed in either order.

**Architecture:** Three independent files, no shared interfaces. `tools/impl/fs.py` moves from "normalize the whole file, edit, un-normalize the whole file" to "match newline-agnostically, splice the matched span, leave every other byte alone." `workspace/memory.py` gains one shared `_resolve_slug` seam that `read_memory`/`delete_memory` use instead of re-slugifying a bare title, plus an injective `_index_title`. `workflows/engine.py` gets a `try`/`finally` so the run always announces.

**Tech Stack:** Python 3.10+, pytest, `uv run` for everything. Ruff (line length 100, `E,F,I,UP,B,SIM,C901`), pyright standard mode.

## Global Constraints

- Run everything through `uv` — `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`/`pytest`.
- `requires-python = ">=3.10"`. No 3.11+ only syntax.
- Ruff line length 100; cyclomatic complexity capped at 10 (`C901`). If a function trips it, extract a named helper — do not add `# noqa: C901`.
- Coverage gate is `--cov-fail-under=90`; the full suite must stay green.
- This codebase's comments explain *why* a non-obvious invariant holds. Every fix below removes or replaces a load-bearing comment — rewrite it, don't delete it.
- Use `uv run pytest --no-cov -n 0 <path>` for single-test runs; the default addopts run parallel with coverage.
- Verify with `uv run ruff check src tests && uv run pyright && uv run pytest` before the final commit of each task.

---

### Task 1: `edit_file` must not rewrite bytes outside the edited span

**Files:**
- Modify: `src/marim_harness/tools/impl/fs.py:415-441` (`_apply_edit`), `:444-467` (`_read_for_edit`), `:470-477` (`_restore_newlines`), `:498-502` (the `edit_file` loop + write)
- Test: `tests/test_fs.py` (add beside `test_edit_file_preserves_crlf_line_endings` at line 227)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `_normalize_with_map(raw: str) -> tuple[str, list[int]]` and `_splice_edit(raw: str, edit: Edit, path: str, index: int, newline: str) -> str`, both module-private to `fs.py`. `_read_for_edit(p: Path) -> tuple[str, str]` keeps its signature but now returns **raw** text (not LF-normalized) as the first element. `_restore_newlines` is deleted.

**Background (read before writing code):** `_read_for_edit` currently does
`text = raw.replace("\r\n", "\n").replace("\r", "\n") if "\r" in raw else raw` and
`newline = "\r\n" if "\r\n" in raw else "\n"`. Two bugs follow: a lone `\r` becomes `\n`
and is never restored, and *any* CRLF in the file makes `_restore_newlines` convert
*every* line to CRLF.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fs.py`:

```python
def test_edit_file_preserves_a_lone_cr(tmp_path: Path):
    """A lone CR is not a line terminator on any platform marim targets. It must
    survive an edit byte-for-byte: normalizing it to LF is a change to a line the
    model never touched, and _restore_newlines cannot tell it apart from a real
    terminator afterwards."""
    p = tmp_path / "lonecr.txt"
    p.write_bytes(b"alpha\rbeta\ngamma\n")
    fs.edit_file(tmp_path, "lonecr.txt", [_edit("gamma", "GAMMA")])
    assert p.read_bytes() == b"alpha\rbeta\nGAMMA\n"


def test_edit_file_does_not_convert_mixed_line_endings(tmp_path: Path):
    """A mostly-LF file with one CRLF line must keep exactly that shape. The old
    code saw one CRLF, set newline='\\r\\n', and rewrote every terminator — a
    whole-file diff the model never requested."""
    p = tmp_path / "mixed.txt"
    p.write_bytes(b"line1\nline2\r\nline3\nline4\n")
    fs.edit_file(tmp_path, "mixed.txt", [_edit("line4", "LINE4")])
    assert p.read_bytes() == b"line1\nline2\r\nline3\nLINE4\n"


def test_edit_file_matches_across_a_crlf_terminator(tmp_path: Path):
    """read_file shows the model LF, so its old_string uses LF. Matching must be
    newline-agnostic: an old_string spanning a CRLF line break still matches, and
    the replacement is written with the file's dominant terminator."""
    p = tmp_path / "span.txt"
    p.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    fs.edit_file(tmp_path, "span.txt", [_edit("alpha\nbeta", "ONE\nTWO")])
    assert p.read_bytes() == b"ONE\r\nTWO\r\ngamma\r\n"


def test_edit_file_inserts_the_local_terminator_not_the_dominant_one(tmp_path: Path):
    """A mostly-CRLF file with one LF line: a newline INSERTED into the LF region
    must arrive as LF. Picking the file's dominant terminator for inserted text is
    the same whole-file-assumption bug at one-line scale."""
    p = tmp_path / "mostly_crlf.txt"
    p.write_bytes(b"a\r\nb\r\nc\n")
    fs.edit_file(tmp_path, "mostly_crlf.txt", [_edit("c", "c\nNEW")])
    assert p.read_bytes() == b"a\r\nb\r\nc\nNEW\n"


def test_edit_file_inserts_crlf_inside_a_crlf_region(tmp_path: Path):
    """The converse: inserting next to CRLF lines must produce CRLF."""
    p = tmp_path / "crlf_region.txt"
    p.write_bytes(b"a\r\nb\r\nc\n")
    fs.edit_file(tmp_path, "crlf_region.txt", [_edit("b", "b\nNEW")])
    assert p.read_bytes() == b"a\r\nb\r\nNEW\r\nc\n"


def test_edit_file_replace_all_preserves_surrounding_bytes(tmp_path: Path):
    """replace_all splices every occurrence and still leaves untouched bytes —
    including a lone CR — exactly as they were."""
    p = tmp_path / "all.txt"
    p.write_bytes(b"x\rfoo\nfoo\r\nbar\n")
    fs.edit_file(
        tmp_path, "all.txt", [fs.Edit(old_string="foo", new_string="Q", replace_all=True)]
    )
    assert p.read_bytes() == b"x\rQ\nQ\r\nbar\n"
```

Note: `_edit` is the existing helper in `tests/test_fs.py`; check its signature and use
`fs.Edit(...)` directly for the `replace_all` case if `_edit` does not take that kwarg.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_fs.py -k "lone_cr or mixed_line or across_a_crlf or replace_all_preserves" -v`

Expected: `test_edit_file_preserves_a_lone_cr` FAILS with the actual bytes
`b'alpha\nbeta\nGAMMA\n'`; `test_edit_file_does_not_convert_mixed_line_endings` FAILS with
`b'line1\r\nline2\r\nline3\r\nLINE4\r\n'`. The other two may already pass — they are
regression guards for the new code path.

- [ ] **Step 3: Add the normalize-with-map helper**

Insert into `src/marim_harness/tools/impl/fs.py` immediately above `_apply_edit`:

```python
def _normalize_with_map(raw: str) -> tuple[str, list[int]]:
    """An LF-normalized view of ``raw``, plus a map from each normalized index to
    the raw index it came from (with a final sentinel entry at ``len(raw)``).

    Only ``\\r\\n`` collapses to ``\\n``. A LONE ``\\r`` is deliberately left as a
    literal character: it is not a line terminator on any platform marim targets,
    and rewriting it is a change to a line the edit never named. The old code
    folded it into ``\\n`` and could not restore it — write-back has no way to
    tell a restored terminator from an original one, so the byte was lost.

    The map is what lets the caller splice: the model's ``old_string`` is matched
    against the normalized view (so an LF snippet matches a CRLF file), then the
    match's span is translated back to raw offsets and only those bytes change."""
    out: list[str] = []
    offsets: list[int] = []
    i, n = 0, len(raw)
    while i < n:
        offsets.append(i)
        if raw.startswith("\r\n", i):
            out.append("\n")
            i += 2
        else:
            out.append(raw[i])
            i += 1
    offsets.append(n)
    return "".join(out), offsets
```

- [ ] **Step 4: Replace `_apply_edit` with the splicing version**

Replace the whole of `_apply_edit` (lines 415-441) with:

```python
def _splice_edit(raw: str, edit: Edit, path: str, index: int, newline: str) -> str:
    """Apply one edit to ``raw`` by splicing the matched span, raising ModelRetry
    (naming the edit) on a bad match. ``index`` is 1-based for readable messages.

    Matching is newline-agnostic (read_file shows the model LF, so its old_string
    is LF) but the WRITE is byte-preserving: only the matched span is replaced, so
    a mixed-ending file keeps every terminator the edit did not name. ``newline``
    is the file's dominant terminator and is applied to INSERTED text only."""
    # An empty old_string is a trap, not a valid edit: ``"".count`` in the text
    # is len+1 (every gap between chars matches), so the ambiguity guard below
    # would tell the model to "set replace_all" — and a model that obeys gets
    # new_string spliced between *every* character of the file: silent, total
    # corruption via the tool's own guidance. Refuse up front.
    if edit.old_string == "":
        raise ModelRetry(
            f"edit {index}: old_string is empty. Provide the exact text to "
            f"replace; to insert, anchor new_string to a surrounding non-empty "
            f"snippet (old_string must appear in the file)."
        )
    norm, offsets = _normalize_with_map(raw)
    old = edit.old_string.replace("\r\n", "\n")
    count = norm.count(old)
    if count == 0:
        raise ModelRetry(
            f"edit {index}: old_string not found in {path}. Read the file and copy "
            f"an exact snippet (note earlier edits in this call may have changed it)."
        )
    if count > 1 and not edit.replace_all:
        raise ModelRetry(
            f"edit {index}: old_string found {count} times in {path}. Add surrounding "
            f"context to make it unique, or set replace_all."
        )
    starts: list[int] = []
    pos = 0
    while (j := norm.find(old, pos)) >= 0:
        starts.append(j)
        pos = j + len(old)
        if not edit.replace_all:
            break
    # Splice from the end so the earlier offsets stay valid as raw shifts.
    for s in reversed(starts):
        start_r, end_r = offsets[s], offsets[s + len(old)]
        local = _local_newline(raw, start_r, end_r, newline)
        new = edit.new_string.replace("\r\n", "\n")
        if local == "\r\n":
            new = new.replace("\n", local)
        raw = raw[:start_r] + new + raw[end_r:]
    return raw
```

- [ ] **Step 4b: Derive the inserted terminator from the match's own neighbourhood**

Using the file's *dominant* terminator for inserted text is a narrower version of the bug
this task fixes: in a mostly-CRLF file, a new line inserted into an LF region would get CRLF.
Add this helper immediately below `_normalize_with_map`:

```python
def _local_newline(raw: str, start: int, end: int, dominant: str) -> str:
    """The terminator to give text inserted at ``raw[start:end]``.

    Prefer the terminator the replaced span actually sits against — the one
    immediately after it, else the one immediately before — and only fall back to
    the file's ``dominant`` terminator when the span touches neither. Using the
    dominant terminator unconditionally reintroduces the bug this module is
    fixing, one line at a time: in a mostly-CRLF file, a line inserted into an LF
    region would silently arrive as CRLF."""
    if raw.startswith("\r\n", end):
        return "\r\n"
    if raw.startswith("\n", end):
        return "\n"
    before = raw.rfind("\n", 0, start)
    if before > 0:
        return "\r\n" if raw[before - 1] == "\r" else "\n"
    return dominant
```

- [ ] **Step 5: Rewrite `_read_for_edit` and delete `_restore_newlines`**

Replace `_read_for_edit` (lines 444-467) with:

```python
def _read_for_edit(p: Path) -> tuple[str, str]:
    """Read ``p`` as UTF-8 for editing, returning ``(raw, newline)``.

    ``raw`` is the file's exact text — NOT normalized. Normalization now happens
    per-match inside ``_splice_edit`` (via ``_normalize_with_map``) so that only
    the matched span is ever rewritten; normalizing the whole file up front and
    un-normalizing on write-back is what silently converted mixed-ending files
    and destroyed lone CRs. We open with ``newline=""`` (not ``read_text``, whose
    ``newline`` kwarg only exists on 3.13+) so the raw terminators stay intact.

    ``newline`` is the file's DOMINANT terminator, applied only to text the edit
    inserts — so a new line added to a CRLF file gets CRLF, while every existing
    terminator is preserved verbatim regardless of the mix.

    Strict UTF-8: unlike read_file (display-only, errors="replace"), edit_file
    reads-modifies-writes, so a lossy decode would round-trip the undecodable
    bytes back as U+FFFD and corrupt regions the edit never touched — a
    UnicodeDecodeError propagates for the caller to refuse."""
    with p.open(encoding="utf-8", newline="") as fh:
        raw = fh.read()
    crlf = raw.count("\r\n")
    lf = raw.count("\n") - crlf
    return raw, "\r\n" if crlf > lf else "\n"
```

Then **delete `_restore_newlines` entirely** (lines 470-477) — the whole-file
re-translation it performed is the bug.

- [ ] **Step 6: Update the `edit_file` call site**

In `edit_file`, replace the loop and write (lines 498-502) with:

```python
    for i, edit in enumerate(edits, 1):
        text = _splice_edit(text, edit, path, i, newline)
    # all-or-nothing on disk too — see write_file. text is raw throughout: each
    # splice preserved every byte outside its own matched span, so no whole-file
    # newline translation is needed (or wanted) here.
    _atomic_write_preserving_mode(p, text)
```

- [ ] **Step 7: Run the new tests**

Run: `uv run pytest --no-cov -n 0 tests/test_fs.py -k "lone_cr or mixed_line or across_a_crlf or replace_all_preserves" -v`
Expected: 4 PASS.

- [ ] **Step 8: Run the whole fs suite for regressions**

Run: `uv run pytest --no-cov -n 0 tests/test_fs.py -v`
Expected: all PASS, including `test_edit_file_preserves_crlf_line_endings` and
`test_edit_file_keeps_lf_line_endings`. If a test referencing `_restore_newlines` fails,
delete that test — the helper is gone by design; note it in the commit message.

- [ ] **Step 9: Gates + commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
git add src/marim_harness/tools/impl/fs.py tests/test_fs.py
git commit -m "fix(edit): splice edits instead of normalizing the whole file

edit_file normalized every CR form to LF on read and re-applied one dominant
terminator on write, so a lone CR was silently lost and a file with any CRLF had
every line converted — byte changes on lines the edit never named. Match against
an LF-normalized view but splice the matched span back into the raw text, so only
the named bytes move. Inserted text still picks up the file's dominant terminator."
```

---

### Task 2: `recall`/`forget` must resolve a title through the index, not by re-slugifying

**Files:**
- Modify: `src/marim_harness/workspace/memory.py:118-143` (`_allocate_slug`), `:158-171` (`read_memory`), `:276-296` (`delete_memory`)
- Test: `tests/test_memory.py` (add beside `test_save_memory_collision_does_not_overwrite_other_entry` at line 473)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `_resolve_slug(scope: MemoryScope, name: str) -> str` — module-private, used by `read_memory`, `delete_memory`, and (indirectly) `_allocate_slug`.

**Background:** `_allocate_slug` correctly gives the second of two like-slugging titles a
`-2` suffix. But `read_memory:165` and `delete_memory:282` still do `slug = _slugify(name)`,
so the *loser's* title resolves to the *incumbent's* file. `forget("auth flow")` therefore
deletes the entry titled `Auth Flow`. `_allocate_slug`'s own docstring calls this a
"Residual" and describes it as the loser being unreachable — it is worse than that: it is a
destructive misresolution. That docstring line must be corrected, not preserved.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_memory.py`:

```python
def test_recall_resolves_a_collided_title_to_its_own_file(tmp_path: Path):
    """Two titles that slugify alike get distinct files. Recalling by the loser's
    title must return the LOSER's body — re-slugifying a bare title resolves to
    the incumbent's file and returns the wrong memory."""
    scope = _scope(tmp_path)
    memory.save_memory(scope, name="auth-flow", title="Auth Flow",
                       description="d1", hook="h1", body="INCUMBENT BODY")
    memory.save_memory(scope, name="auth-flow", title="auth flow",
                       description="d2", hook="h2", body="LOSER BODY")
    assert "LOSER BODY" in memory.read_memory(scope, "auth flow")
    assert "INCUMBENT BODY" in memory.read_memory(scope, "Auth Flow")


def test_forget_deletes_the_named_memory_not_its_slug_twin(tmp_path: Path):
    """forget() by the loser's title must delete the loser. Before this fix it
    re-slugified to the incumbent's slug and destroyed the wrong file — the tool
    docstring tells the model to pass 'its title or slug, as shown in the index',
    and the index shows exactly this title."""
    scope = _scope(tmp_path)
    memory.save_memory(scope, name="auth-flow", title="Auth Flow",
                       description="d1", hook="h1", body="INCUMBENT BODY")
    memory.save_memory(scope, name="auth-flow", title="auth flow",
                       description="d2", hook="h2", body="LOSER BODY")
    assert memory.delete_memory(scope, "auth flow") is True
    assert (tmp_path / "auth-flow.md").exists()          # incumbent survives
    assert not (tmp_path / "auth-flow-2.md").exists()    # loser is gone
    assert "INCUMBENT BODY" in memory.read_memory(scope, "Auth Flow")


def test_recall_and_forget_still_accept_a_bare_slug(tmp_path: Path):
    """The slug path must keep working — the index displays the slug and the tool
    docstring accepts either form."""
    scope = _scope(tmp_path)
    memory.save_memory(scope, name="auth-flow", title="Auth Flow",
                       description="d1", hook="h1", body="INCUMBENT BODY")
    memory.save_memory(scope, name="auth-flow", title="auth flow",
                       description="d2", hook="h2", body="LOSER BODY")
    assert "LOSER BODY" in memory.read_memory(scope, "auth-flow-2")
    assert memory.delete_memory(scope, "auth-flow-2") is True


def test_recall_falls_back_to_slugify_when_the_index_is_missing(tmp_path: Path):
    """A memory file with no index line (hand-created, or a corrupt MEMORY.md)
    must still resolve by slugifying — the index is an optimization, not the
    only source of truth."""
    scope = _scope(tmp_path)
    (tmp_path / "orphan.md").write_text("ORPHAN BODY", encoding="utf-8")
    assert "ORPHAN BODY" in memory.read_memory(scope, "orphan")
```

`_scope` is the existing helper in `tests/test_memory.py`; check its name and signature
and match the file's established construction pattern for a `MemoryScope`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_memory.py -k "collided_title or slug_twin or bare_slug or index_is_missing" -v`
Expected: `test_recall_resolves_a_collided_title_to_its_own_file` FAILS (returns the
incumbent's body); `test_forget_deletes_the_named_memory_not_its_slug_twin` FAILS
(`auth-flow.md` is gone, `auth-flow-2.md` still exists).

- [ ] **Step 3: Add the shared resolver**

Insert into `src/marim_harness/workspace/memory.py` immediately above `_allocate_slug`:

```python
def _resolve_slug(scope: MemoryScope, name: str) -> str:
    """The slug ``name`` refers to — ``name`` may be a stored slug or an entry's
    title, both of which the tool docstrings accept.

    Resolution order matters. An exact SLUG match wins first, so the index's own
    displayed link always resolves to itself. Then a TITLE match, which is the
    whole point of this helper: two distinct titles can slugify alike, and the
    collision loser lives at ``base-2``. Re-slugifying a bare title (what
    read_memory/delete_memory used to do) sends the loser's title to the
    INCUMBENT's file — so ``forget`` deleted a memory the user never named.

    Falls back to ``_slugify`` when nothing matches, so a memory whose index line
    is missing (hand-created file, corrupt MEMORY.md) still resolves."""
    entries = _index_entries(scope)
    for _etitle, eslug in entries:
        if eslug == name:
            return eslug
    wanted = _index_title(name)
    for etitle, eslug in entries:
        if etitle == wanted:
            return eslug
    return _slugify(name)
```

- [ ] **Step 4: Use it in `read_memory` and `delete_memory`**

In `read_memory`, change `slug = _slugify(name)` to `slug = _resolve_slug(scope, name)`,
and replace the docstring's "both slugify to the same file" / "both slugify to the stored
filename" claims with:

```python
    """Return the full text of a memory file by name — its title or its slug.
    Memory files live in marim's own dirs — global is outside the workspace — so
    this reads them directly rather than through the workspace-sandboxed read_file
    tool. The name is resolved through the index (see _resolve_slug) rather than
    re-slugified, because two distinct titles can slugify alike and the collision
    loser lives at a suffixed slug. Returns a notice if no file matches."""
```

In `delete_memory`, change `slug = _slugify(name)` to `slug = _resolve_slug(scope, name)`
and replace the docstring's parenthetical `(title or slug — both slugify to the stored
filename)` with `(its title or its slug; resolved through the index — see _resolve_slug)`.

- [ ] **Step 5: Have `_allocate_slug` reuse the same title lookup, and fix its docstring**

In `_allocate_slug`, delete the now-false final paragraph:

```
    (Residual: read_memory/delete_memory re-slugify a bare title to the base, so a
    loser is not reachable by its title — only by its index slug.)
```

and replace it with:

```
    read_memory/delete_memory resolve through _resolve_slug, so a loser IS
    reachable by its own title — this allocator and those two readers must agree
    on the title→slug mapping or ``forget`` deletes the wrong file.
```

- [ ] **Step 6: Run the new tests**

Run: `uv run pytest --no-cov -n 0 tests/test_memory.py -k "collided_title or slug_twin or bare_slug or index_is_missing" -v`
Expected: 4 PASS.

- [ ] **Step 7: Run the whole memory suite**

Run: `uv run pytest --no-cov -n 0 tests/test_memory.py -v`
Expected: all PASS, including `test_save_memory_collision_does_not_overwrite_other_entry`.

- [ ] **Step 8: Gates + commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
git add src/marim_harness/workspace/memory.py tests/test_memory.py
git commit -m "fix(memory): resolve recall/forget through the index, not by re-slugifying

Two titles that slugify alike get distinct files, but read_memory and
delete_memory still did _slugify(name) — so the loser's title resolved to the
incumbent's file and forget() destroyed a memory the user never named. Add
_resolve_slug (exact slug, then title, then slugify fallback) and use it in both."
```

---

### Task 3: `_index_title` must not map two distinct titles onto one entry

**Files:**
- Modify: `src/marim_harness/workspace/memory.py:91-100` (`_index_title`), plus the title
  comparison in `_resolve_slug`/`_allocate_slug` from Task 2
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: `_resolve_slug` from Task 2.
- Produces: `_legacy_index_title(title: str) -> str` and `_title_matches(stored: str, title: str) -> bool`, both module-private.

**Background:** `_index_title` *deletes* `[ ] ( )` so that a `](` in a title cannot forge a
second markdown link and defeat `_ENTRY_LINK_RE`'s first-link anchor. But deletion is not
injective: `a(b)` and `ab` both become `ab`, so `_allocate_slug` reuses the first entry's
slug for the second title and the second save overwrites the first — the exact loss
`_allocate_slug` exists to prevent. Escaping instead of deleting preserves the title and
keeps the forgery defence (an escaped `\[x\]\(y\)` is not a link).

- [ ] **Step 1: Write the failing test**

```python
def test_titles_differing_only_in_brackets_get_separate_files(tmp_path: Path):
    """_index_title used to DELETE []() , so 'a(b)' and 'ab' collapsed to the same
    index title and the second save overwrote the first — the very loss
    _allocate_slug was added to prevent. Escaping keeps the mapping injective."""
    scope = _scope(tmp_path)
    memory.save_memory(scope, name="n1", title="a(b)",
                       description="d1", hook="h1", body="BODY ONE")
    memory.save_memory(scope, name="n2", title="ab",
                       description="d2", hook="h2", body="BODY TWO")
    assert "BODY ONE" in memory.read_memory(scope, "a(b)")
    assert "BODY TWO" in memory.read_memory(scope, "ab")


def test_a_markdown_link_in_a_title_cannot_forge_a_second_entry_link(tmp_path: Path):
    """The original reason _index_title existed: a title containing '](' would
    forge a second link on the entry line and _ENTRY_LINK_RE (first-link anchored)
    would capture the wrong slug. Escaping must keep that closed."""
    scope = _scope(tmp_path)
    memory.save_memory(scope, name="tricky", title="see [x](evil.md)",
                       description="d", hook="h", body="TRICKY BODY")
    entries = memory._index_entries(scope)
    assert [slug for _t, slug in entries] == ["tricky"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov -n 0 tests/test_memory.py -k "differing_only_in_brackets or forge_a_second" -v`
Expected: `test_titles_differing_only_in_brackets_get_separate_files` FAILS — `a(b)`'s
file was overwritten by the second save.

- [ ] **Step 3: Make `_index_title` escape instead of delete**

Replace `_index_title` with:

```python
def _index_title(title: str) -> str:
    """Sanitize a title for the one-line index entry: collapse to a single line
    and BACKSLASH-ESCAPE markdown link punctuation ``[]()``.

    A title containing ``](`` — e.g. a pasted ``see [x](y.md)`` — would forge a
    second ``](slug.md)`` link on the entry line, so ``_ENTRY_LINK_RE`` (which
    anchors on the FIRST such link) captures the wrong slug and the upsert/delete
    dedup misfires. Escaping neutralizes the forgery exactly as deleting did —
    ``\\[x\\]\\(y.md\\)`` is not a link — while staying INJECTIVE. Deleting was
    not: ``a(b)`` and ``ab`` both became ``ab``, so _allocate_slug handed the
    second title the first's slug and the save overwrote it."""
    return _single_line(re.sub(r"([\[\]()])", r"\\\1", title or ""))


def _legacy_index_title(title: str) -> str:
    """The pre-escaping sanitizer, which DELETED ``[]()``. Kept solely so
    _title_matches can still recognize entries written by an older marim: without
    it, re-saving a bracket-bearing title would fail to find its own index line
    and allocate a fresh slug, orphaning the existing file."""
    return _single_line(re.sub(r"[\[\]()]", "", title or ""))


def _title_matches(stored: str, title: str) -> bool:
    """Whether an index entry's stored title refers to ``title`` — under the
    current escaping sanitizer or the legacy deleting one (see
    _legacy_index_title)."""
    return stored == _index_title(title) or stored == _legacy_index_title(title)
```

- [ ] **Step 4: Use `_title_matches` at both comparison sites**

In `_resolve_slug` (Task 2), replace:

```python
    wanted = _index_title(name)
    for etitle, eslug in entries:
        if etitle == wanted:
            return eslug
```

with:

```python
    for etitle, eslug in entries:
        if _title_matches(etitle, name):
            return eslug
```

In `_allocate_slug`, replace:

```python
    wanted = _index_title(title)
    entries = _index_entries(scope)
    for etitle, eslug in entries:
        if etitle == wanted:
            return eslug
```

with:

```python
    entries = _index_entries(scope)
    for etitle, eslug in entries:
        if _title_matches(etitle, title):
            return eslug
```

- [ ] **Step 5: Run the new tests**

Run: `uv run pytest --no-cov -n 0 tests/test_memory.py -k "differing_only_in_brackets or forge_a_second" -v`
Expected: 2 PASS.

- [ ] **Step 6: Run the whole memory suite**

Run: `uv run pytest --no-cov -n 0 tests/test_memory.py -v`
Expected: all PASS. A test asserting the *deleted*-bracket index text will now fail —
update its expectation to the escaped form and say so in the commit message.

- [ ] **Step 7: Gates + commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
git add src/marim_harness/workspace/memory.py tests/test_memory.py
git commit -m "fix(memory): escape index-title punctuation instead of deleting it

Deleting []() was not injective: 'a(b)' and 'ab' both became 'ab', so
_allocate_slug reused the first entry's slug and the second save overwrote it —
the loss _allocate_slug exists to prevent. Escape instead (still not a link, so
the link-forgery defence holds) and match stored titles under both the new and
legacy sanitizers so existing indexes keep resolving."
```

---

### Task 4: A failing spill write must not strand the workflow card or discard the result

**Files:**
- Modify: `src/marim_harness/workflows/engine.py:253-255` (the success path in `run`),
  `:429-431` and `:441-443` (the two unguarded `write_spill` calls in `_shape`)
- Test: `tests/test_workflows.py` (confirm the filename with
  `ls tests | grep -i workflow`)

**Interfaces:**
- Consumes: nothing from Tasks 1-3.
- Produces: no new symbols; `_shape` keeps its signature.

**Background:** `run` does `shaped = self._shape(...)` then `self._announce_done(...)` with
no `try`/`finally`. `_shape` calls `write_spill`, which does `mkdir` + `atomic_write_text`
and can raise `OSError`. `tools/workflow_tools.py:140` is a bare `return await runner(...)`,
so the error propagates: a workflow that ran several expensive sub-agents loses its result
and the TUI card stays "running" forever. Every other exit in `run` — abort, timeout,
`MontyRuntimeError` — already announces.

- [ ] **Step 1: Write the failing test**

```python
async def test_spill_write_failure_still_announces_and_returns(tmp_path, monkeypatch):
    """A failing spill write must not strand the card or lose the result. The
    payload is the product of sub-agent runs that already cost real tokens, so
    degrade to the capped in-band text rather than raising into the turn."""
    announced: list[tuple[str, bool]] = []

    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(engine, "write_spill", boom)
    # Build the engine the way the existing tests in this file do, with an
    # announce hook recording (outcome, failed) into `announced`, and run a
    # script whose final expression exceeds MAX_RESULT_CHARS so _shape spills.
    result = await _run_script(f"'x' * {engine.MAX_RESULT_CHARS + 1000}")

    assert announced, "the card was never settled — it would spin forever"
    assert result, "the workflow's result was discarded"
```

Mirror the existing engine-construction and announce-capture helpers already in that test
file rather than inventing new ones; `_run_script` above stands for whatever the file's
established runner helper is called.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov -n 0 tests/test_workflows.py -k spill_write_failure -v`
Expected: FAIL with `OSError: read-only file system` escaping the call — `announced` is
empty.

- [ ] **Step 3: Make the two `write_spill` calls best-effort**

In `_shape`, wrap both call sites. Replace each of:

```python
            if spill is not None:
                write_spill(self.deps.workspace.root, spill_path, rel, spill)
```

with:

```python
            if spill is not None:
                # Best-effort: the payload is the product of sub-agent runs that
                # already cost real tokens, so a failed spill degrades to the
                # capped in-band text rather than discarding the whole result and
                # leaving the card spinning. The capped text is already in `text`.
                try:
                    write_spill(self.deps.workspace.root, spill_path, rel, spill)
                except OSError as exc:
                    logger.warning("workflow spill write failed: %s", exc, exc_info=True)
```

Apply the same wrapper at both occurrences (the `value is None and printed.strip()` branch
and the `shape_result` branch). Confirm `logger` is already bound at module scope in
`engine.py`; if not, add `logger = logging.getLogger(__name__)` beside the other
module-level definitions.

- [ ] **Step 4: Guarantee the announce on the success path**

Replace lines 253-255 of `run`:

```python
        shaped = self._shape(value, tool_call_id, prints.text())
        self._announce_done(tool_call_id, shaped, failed=False)
        return shaped
```

with:

```python
        # Every other exit above announces before returning; this one must too,
        # even if _shape raises. An unannounced card spins "running" forever —
        # the TUI has no other settle signal for a workflow.
        try:
            shaped = self._shape(value, tool_call_id, prints.text())
        except Exception as exc:
            outcome = f"Workflow completed but its result could not be rendered: {exc}"
            self._announce_done(tool_call_id, outcome, failed=True)
            return outcome
        self._announce_done(tool_call_id, shaped, failed=False)
        return shaped
```

- [ ] **Step 5: Run the new test**

Run: `uv run pytest --no-cov -n 0 tests/test_workflows.py -k spill_write_failure -v`
Expected: PASS.

- [ ] **Step 6: Run the whole workflow suite**

Run: `uv run pytest --no-cov -n 0 tests/test_workflows.py -v`
Expected: all PASS.

- [ ] **Step 7: Gates + commit**

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
git add src/marim_harness/workflows/engine.py tests/test_workflows.py
git commit -m "fix(workflows): never lose a result or strand a card on a spill failure

_shape's write_spill calls were unguarded and run's success path had no
try/finally, so an OSError (read-only workspace, ENOSPC) propagated out of the
tool: the computed result — the product of several sub-agent runs — was
discarded and the TUI card stayed 'running' forever. Spill is now best-effort
(degrade to the capped in-band text) and the success path always announces."
```

---

## Self-Review

**Spec coverage.** Review majors W-2 (Task 1), W-1 (Task 2), ND1 (Task 3), SW3 (Task 4).
Deliberately out of scope, tracked for plan 5: the `_allocate_slug` read-modify-write race
outside `file_lock` (`memory.py:236` vs `:198`) — it is a concurrency minor, needs a
different fix shape (widening the lock to cover allocation), and folding it in would make
Task 2's diff two unrelated changes a reviewer would have to gate together.

**Placeholder scan.** No TBDs. Three places name an existing test helper without repeating
its body — `_edit` (`tests/test_fs.py`), `_scope` (`tests/test_memory.py`), and the engine
runner in `tests/test_workflows.py` — each with an instruction to check the real signature
first. That is deliberate: inventing a fixture that already exists would break the file's
conventions.

**Type consistency.** `_normalize_with_map` returns `tuple[str, list[int]]` and is consumed
only by `_splice_edit`. `_read_for_edit` keeps `tuple[str, str]` but its first element
changes meaning (raw, not normalized) — the docstring says so and `_restore_newlines`, its
only other collaborator, is deleted in the same task. `_resolve_slug` (Task 2) is amended by
Task 3 to call `_title_matches`; execute Task 2 before Task 3.
