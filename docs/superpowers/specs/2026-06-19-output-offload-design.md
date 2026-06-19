# Unified large-output offload for grep / glob / tree / bash — design

**Date:** 2026-06-19
**Status:** Approved (design); implementation plan to follow.

## Goal

Make `grep`, `glob`, `tree`, and `bash` handle large output the same way `fetch_url`
already does: when the result is large, write the **full** result to a gitignored
file and return a compact handle + preview, so nothing is silently lost and the
turn's context isn't flooded. This replaces today's lossy, inconsistent caps.

## Current state (the problem)

- `fetch_url`: **offloads** bodies over `_INLINE_CHAR_LIMIT` (50k chars) to
  `.marim/fetch/<digest>.md`, returns a handle + 40-line preview. Lossless.
- `grep` (`tools/fs.py`): **truncates** at `_MAX_GREP_HITS = 200` → appends
  `(truncated)`. Lossy.
- `tree` (`tools/fs.py`): **truncates** at `_MAX_TREE_ENTRIES = 500` → `(truncated)`.
  Lossy.
- `glob_files` (`tools/fs.py`): **uncapped** — can flood context with thousands of
  paths.
- `run_bash` (`tools/shell.py`): **drops the middle** of output at
  `_DEFAULT_MAX_OUTPUT = 20_000` chars. Lossy.
- `read_file` (`tools/fs.py`): already paginates via `offset`/`limit`. Out of scope.

## Scope

- **In scope:** `grep`, `glob_files`, `tree`, `run_bash`, and a new shared offload
  helper. Update `.gitignore`.
- **Out of scope:** `read_file` (already paginated), `fetch_url`'s public behavior
  (it keeps its richer title/URL handle; it may be refactored to share the helper's
  core but its output format is unchanged), `web_search`.

## Components

### 1. Shared offload helper (`src/marim_harness/tools/offload.py`, new)

One implementation and one format for all offloading tools:

```
_INLINE_CHAR_LIMIT = 50_000      # ~12k tokens; below this, return inline
_MAX_OUTPUT_BYTES  = 5_000_000   # hard ceiling backstop (mirrors fetch _MAX_BYTES)
_PREVIEW_LINES     = 40
_OUTPUT_DIR        = (".marim", "output")

def offload_if_large(
    content: str,
    *,
    kind: str,                       # "grep" | "glob" | "tree" | "bash"
    key: str,                        # the tool's query/args, for the digest
    workspace_root: Optional[Path],
    capped: bool = False,            # True when the producer hit the hard ceiling
) -> str
```

Behavior:
- `workspace_root is None` **or** `len(content) <= _INLINE_CHAR_LIMIT` → return
  `content` unchanged (small results and no-workspace contexts pay nothing).
- Otherwise: write the full `content` to
  `<workspace_root>/.marim/output/<kind>-<digest>.txt`, where
  `digest = sha256((kind + "\0" + key).encode())[:16]` (re-running the same
  search reuses the file). Return a handle:

  ```
  ⚠️ Large <kind> result (<N> chars, <M> lines) — full output saved to
  `<relpath>`. Read more with read_file (it paginates) or grep.
  [if capped:] ⚠️ Output hit the <_MAX_OUTPUT_BYTES>-byte ceiling; the file holds
  what was collected.
  --- preview (first <k> lines) ---
  <first _PREVIEW_LINES lines of content>
  ```

The helper writes via `dest.parent.mkdir(parents=True, exist_ok=True)` then
`dest.write_text(content)`, matching `fetch._offload`.

### 2. Threshold + hard ceiling

- The shared `_INLINE_CHAR_LIMIT` (50k chars) is the single inline/offload boundary,
  consistent with fetch.
- The old lossy caps (`_MAX_GREP_HITS`, `_MAX_TREE_ENTRIES`, the bash middle-drop)
  are **removed**.
- The hard ceiling `_MAX_OUTPUT_BYTES` (5 MB) bounds memory/work now that the caps
  are gone: a producer stops collecting once its accumulated output would exceed the
  ceiling, sets `capped=True`, and offloads what it has. 5 MB is ~100× the inline
  limit, so it only trips on pathological results.

### 3. Per-tool integration

- **grep** (`tools/fs.py`): collect all `relpath:line:text` hits; stop early only
  when the running output size would exceed `_MAX_OUTPUT_BYTES` (then `capped=True`).
  Return `offload_if_large("\n".join(out), kind="grep", key=f"{pattern}\0{path}",
  workspace_root=root, capped=capped)`. `(no matches)` stays inline.
- **glob** (`glob_files`): collect all matches; same hard-ceiling guard; offload with
  `kind="glob"`, `key=pattern`. `(no matches)` stays inline.
- **tree**: build the full listing; same hard-ceiling guard; offload with
  `kind="tree"`, `key=f"{path}\0{depth}"`.
- **bash** (`run_bash`): `communicate()` already buffers full stdout. Replace the
  middle-drop with: if the decoded output exceeds `_INLINE_CHAR_LIMIT`, offload the
  full output (`kind="bash"`, `key=command`); else return it inline unchanged. The
  `exit N\n` prefix stays on the inline path; for the offloaded path the handle
  notes the exit code (e.g. prepend `exit N` before the handle) so the agent still
  sees it.
  - **Background jobs** (`BackgroundProcess`): the live `output()` poll keeps the
    current head+tail truncation — it is a streaming preview with no stable file
    yet. Only the **final** `wait()` result is run through `offload_if_large`. This
    asymmetry is intentional and documented in the code.

### 4. Files & gitignore

- Offloaded results live under `.marim/output/`, gitignored. Add `.marim/output/`
  to `.gitignore` (alongside the existing `.marim/fetch/`).
- The helper must reference each tool's workspace_root the same way the tool already
  receives it (grep/glob/tree get `root`; `run_bash` gets a workspace path / cwd).

### 5. fetch reuse (optional, DRY)

`fetch._offload` and the new helper share the "write file + build preview" core.
Refactor the common core into the shared helper and have `fetch._offload` call it
with a fetch-specific header (title + `Fetched <url>`), so there is one file-writing
+ preview implementation. `fetch_url`'s observable output is unchanged.

## Data flow

1. Tool produces output, bounded by `_MAX_OUTPUT_BYTES`.
2. `offload_if_large` decides: small → inline; large → write
   `.marim/output/<kind>-<digest>.txt`, return handle + preview.
3. Agent reads the file with `read_file` (paginated) or narrows with `grep`.

## Error handling

- No workspace root → never offload; return inline (small) or, for safety, the
  inline-capped content. (Producers still honor the hard ceiling so a no-workspace
  context can't be flooded by an unbounded result; when capped without a workspace,
  return the collected content with a short `(output capped at N bytes)` note.)
- Write failure (OSError) → fall back to returning the content inline (truncated to
  the inline limit with a note) rather than raising, so a tool never fails purely
  because offload couldn't write.
- `(no matches)` / empty output → always inline.

## Testing

- **Shared helper:** small content → returned unchanged; large content → file
  written with the FULL content, handle names the relative path, preview present;
  `workspace_root=None` → inline; `capped=True` → handle notes the ceiling; digest
  is stable for the same `(kind, key)` and writes to `.marim/output/`.
- **grep:** small search inline (existing behavior); large search → offloaded file
  contains every hit, no `(truncated)` marker; hard-ceiling path caps + notes.
- **glob:** large match set offloaded with every path; small inline.
- **tree:** large tree offloaded; small inline.
- **bash:** large sync output offloaded (full output in file, exit code visible);
  small output inline with `exit N` unchanged; background `output()` still streams
  head+tail truncated.
- **gitignore:** `.marim/output/` is ignored.

## Build order

1. Shared `offload.py` helper + tests.
2. `.gitignore` entry.
3. grep → offload (remove `_MAX_GREP_HITS`).
4. glob → offload (add hard-ceiling guard).
5. tree → offload (remove `_MAX_TREE_ENTRIES`).
6. bash → offload (replace middle-drop; background nuance).
7. Refactor `fetch._offload` onto the shared core (optional, last; keep fetch output identical).
