# grep optimization (pure-Python) — design

**Date:** 2026-06-19
**Status:** Approved (design); implementation plan to follow.

## Goal

Make `grep` fast on large folders. Today it is slow because it walks and reads
*every* file under the search root — including `.git`, `node_modules`, `.venv`,
`__pycache__`, etc. — and reads each file fully (binaries included) before
deciding to skip it.

## Problem (current `grep` in `src/marim_harness/tools/fs.py`)

```python
files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
for f in files:
    ... resolve_in_workspace(root, rel) ...           # per-file path resolve
    for i, line in enumerate(f.read_text().splitlines(), 1):  # full read incl. binaries
        ...
```

- `rglob("*")` descends into and materializes every entry, including huge noise
  dirs. `grep` does NOT use the noise-dir skip that `tree` already has.
- `read_text()` reads whole files (binary files are only skipped *after* a full
  read raises `UnicodeDecodeError`).
- `resolve_in_workspace` runs for every file, not just the rare symlink.

## Approach (chosen: pure-Python, no new dependency)

Rejected alternatives: delegating to `ripgrep` (would silently honor `.gitignore`
— changing the result set —, use Rust-flavored regex instead of Python `re`, and
depend on an external binary). The pure-Python fix removes the waste while keeping
the exact regex semantics and result set (minus noise dirs).

### Changes to `grep`

1. **Prune noise dirs during the walk.** Replace `rglob("*")` with a streaming
   `os.walk(base)` that prunes in place:
   ```python
   dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS]
   ```
   `_NOISE_DIRS` is the existing `_TREE_SKIP_DIRS` set, lifted to a shared module
   constant referenced by both `tree` and `grep` (`.git`, `node_modules`, `.venv`,
   `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `dist`, `build`,
   `.egg-info`, `.worktrees`). `os.walk` does not follow symlinked directories by
   default, so the walk cannot wander outside the tree.

2. **Binary sniff before reading.** For each candidate file, read the first 8 KB
   as bytes; if it contains a NUL byte, skip the file. Avoids fully reading
   binaries.

3. **Symlink-escape guard only when needed.** Keep `resolve_in_workspace`, but
   call it only for actual symlinks (`f.is_symlink()`). Non-symlinks (the common
   case) skip the cost; the security property (no in-tree `evil -> /etc/passwd`
   read) is preserved.

4. **Stream lines.** Iterate the file with `open(path, errors="replace")` line by
   line instead of `read_text().splitlines()` — lower memory, and it combines with
   the existing offload size-guard to stop early.

### Unchanged

- The compiled Python `re` pattern and `relpath:line:text` output format.
- The `MAX_OUTPUT_BYTES` running-size guard + `offload_if_large` (large results
  still offload to `.marim/output/grep-<digest>.txt`).
- `(no matches)` for empty results.
- The single-file path (`grep` on a file): same, plus the binary sniff.

## Behavior changes (both improvements; document them)

- `grep` no longer searches inside noise dirs (`.git`, `node_modules`, `.venv`,
  `__pycache__`, `dist`, `build`, …). Hidden dirs that are NOT noise (e.g.
  `.github`, `.vscode`) are still searched.
- `grep` skips binary (NUL-containing) files.
- `grep` does NOT read `.gitignore`; result set is otherwise identical to today.

## Error handling

- Unreadable file (OSError on open/read) → skip (as today).
- Decode issues → `errors="replace"` (no crash); binary already skipped by the sniff.

## Testing

- Existing grep tests stay green (`relpath:line:text`, `(no matches)`,
  symlink-escape blocked, small inline, large offload).
- New tests:
  - a match inside `node_modules/` (or `.git/`) is NOT returned.
  - a match in a non-noise dotfile dir (`.github/`) IS returned.
  - a binary file containing a NUL byte plus a "matching" byte sequence is skipped.
  - a deeply nested match (e.g. `a/b/c/d.txt`) is still found via `os.walk`.
  - the symlink-escape guard still blocks an in-tree symlink pointing outside.
  - a large result still offloads (size-guard intact).

## Build order

1. Lift `_TREE_SKIP_DIRS` to a shared `_NOISE_DIRS` (used by `tree` and `grep`).
2. Rewrite `grep` to walk-with-prune + binary sniff + symlink-only resolve +
   streamed lines, preserving the offload size-guard and output.
