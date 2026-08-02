# `marim import claude` — memory slice

Date: 2026-08-02
Status: approved, ready for implementation planning

## Problem

marim's memory format deliberately mirrors Claude Code's: a `MEMORY.md` index of
one-line pointers plus one `<slug>.md` per fact carrying YAML frontmatter
(`name`, `description`, `metadata.type`) and a markdown body. A user switching
from Claude Code to marim has a populated memory store and no way to carry it
over except by hand.

ROADMAP lists `marim import claude` as one command covering memory, hooks,
skills, MCP servers and `AGENTS.md`/`CLAUDE.md`. This spec covers **only the
memory store**. The other pieces are separate slices with their own trust and
format questions.

Two adjacent things explicitly do *not* need importing:

- **Project instruction files.** `runtime/instructions.py` already lists
  `CLAUDE.md` in `_PROJECT_FALLBACK_FILES`, so a project's `CLAUDE.md` is read
  in place with no import step.
- **User-level `~/.claude/CLAUDE.md`.** Out of scope here; the marim equivalent
  is `~/.config/marim/AGENTS.md` and a future slice can copy it.

## Source and target

Claude Code stores memory per *project directory*, outside the repo, at
`<claude-config>/projects/<cwd-slug>/memory/`. `<claude-config>` is
`$CLAUDE_CONFIG_DIR` when set, else `~/.claude`.

The slug replaces every `/` and `.` in the absolute path with `-`. Verified
against real directories on disk:

| path | slug |
| --- | --- |
| `/home/x/Projects/marim.dev/marim-harness` | `-home-x-Projects-marim-dev-marim-harness` |
| `/home/x/.local/share/fastcloud-studio` | `-home-x--local-share-fastcloud-studio` |

The leading `-` (from the leading `/`) and the doubled `--` (from `/.`) both
fall out of that single rule.

Target is marim's **project scope**: `<workspace>/.marim/memory`, i.e.
`memory.project_scope(workspace_root)`. This matches the semantics — Claude
keyed the memories to that working directory — at the cost that `.marim/` is
inside the repo and committable. See *Privacy warning* below.

Global scope is not a target and there is no `--scope` flag. Add one only if a
user asks.

## Command surface

```
marim import claude [workspace]
  --from PATH   explicit Claude memory dir (or the project dir containing it)
  --apply       perform the import (default: dry-run, writes nothing)
  --force       overwrite slugs that already exist in the target
```

`workspace` defaults to `.`.

Registered as a management keyword in `interfaces/cli/router.py`: add `"import"`
to `_MANAGEMENT` and `{"import": "import_cmd"}` to `_MODULE_NAMES`. The module
is named `import_cmd.py` because `import` is a Python keyword — this is the same
seam already used for `trust` → `trust_cmd`.

`main(argv, *, out=None, err=None) -> int`, matching `trust_cmd`, so tests drive
it in-process with captured streams. `out`/`err` default to `None` and resolve
to the current `sys.stdout`/`sys.stderr` inside the call — never bound at
def-time, for the reason documented in `trust_cmd.main`.

Source resolution order:

1. `--from PATH` if given. If `PATH` contains a `memory/` subdir, use that;
   if `PATH` is itself a memory dir, use it directly.
2. Otherwise derive the slug from the resolved workspace root.

On a miss, print every `<claude-config>/projects/*/memory` that exists, with its
memory count, and exit `1` telling the user to pass `--from`. A worktree gets
its own Claude project dir, so `--from` is also the answer there.

## Format bridge

A Claude memory file carries `name` (the slug), `description`, `metadata.type`
and the body. It does **not** carry a title — the title lives in Claude's
`MEMORY.md` index line, in the identical shape marim writes:

```
- [Title](slug.md) — hook
```

marim's `save_memory` requires `title` (it keys the upsert and renders the index
line). So the importer parses the source `MEMORY.md` with marim's own
`_ENTRY_LINK_RE` to recover each slug's title, falling back to the slug itself
for an orphan file with no index entry.

That requires promoting `_index_entries` to a public `index_entries` in
`workspace/memory.py`. The importer is a legitimate second reader of the index
format; sharing the one regex is better than a second parser that can drift.
The private alias is dropped — there is one internal caller (`_allocate_slug`).

Everything else lands for free by writing through `save_memory`:

- `_render_frontmatter` coerces an unrecognized `metadata.type` to `project`.
  Claude uses the same four values (`user`/`feedback`/`project`/`reference`),
  so this is a no-op in practice and a safety net otherwise.
- `_allocate_slug` reuses an existing entry's slug on a title match and
  disambiguates two distinct titles that slugify alike.
- `_upsert_index_line` keeps the target `MEMORY.md` correct under its advisory
  lock and atomic write.

**Dropped on purpose:** Claude's extra frontmatter keys `node_type`,
`originSessionId`, `modified`. marim reads none of them, and adding pass-through
would fork the format the two tools currently share. This loses per-memory
provenance; the dry-run output naming the source dir is the compensation.

No trust gate. The importer reads inert markdown and executes nothing, unlike
project hooks or `.marim/mcp.json` servers.

## Conflict policy

Skip by default; `--force` overwrites.

"Already exists" is decided by the presence of `<target>/<slug>.md`, not by the
target index (which could be stale) — the same reasoning as
`memory._link_saved`. Each skip is reported by slug so the user can inspect the
file and re-run with `--force` if Claude's version should win.

The default never destroys a memory marim's own `remember` tool wrote.

## Module split

Follows the repo's pure-decision / effectful-IO convention. New module
`workspace/claude_import.py`:

- `claude_project_slug(path: Path) -> str` — pure.
- `claude_config_dir(*, env) -> Path` — pure; `$CLAUDE_CONFIG_DIR` or `~/.claude`.
- `claude_memory_dir(workspace: Path, *, config_dir: Path) -> Path` — pure path math.
- `parse_memory_file(text: str) -> ImportedMemory | None` — pure. Splits the
  YAML frontmatter, pulls `name`/`description`/`metadata.type`, returns the body.
  Returns `None` for a file with no parseable frontmatter.
- `plan_import(sources, existing_slugs, *, force) -> list[PlannedImport]` — pure.
  Each entry is `(action, slug, title)` with action ∈ `import` / `overwrite` /
  `skip`.
- `read_source(memory_dir) -> list[ImportedMemory]` — I/O: list `*.md` (excluding
  `MEMORY.md`), read each, parse, attach the title recovered from the index.
- `apply_plan(plan, sources, scope) -> ImportResult` — the only writer;
  delegates each write to `memory.save_memory`.

`ImportedMemory` and `PlannedImport` are frozen dataclasses.

`interfaces/cli/import_cmd.py` is thin wiring over those — argparse, source
resolution, rendering, exit code — mirroring how `fs_tools.py` sits over
`tools/impl/fs.py`.

## Output

```
source: ~/.claude/projects/-home-…-marim-harness/memory  (12 memories)
target: /home/…/marim-harness/.marim/memory

  import   provider-error-dump-flake      Provider-error dump test flake
  import   ci-load-causes-timing-flakes   Single-leg CI flake = runner load
  skip     session-model-overrides-env    already present — use --force

11 to import, 1 skipped.  Dry run — re-run with --apply to write.
```

With `--apply` the trailing line becomes `11 imported, 1 skipped.` and the
dry-run sentence is dropped.

### Privacy warning

When `--apply` runs and the workspace is a git repo whose `.marim/` is not
gitignored, print a one-line warning naming the target as repo-tracked, before
the summary. Importing a personal Claude memory store into a committed
directory is the one way this command can surprise someone.

Most projects ignore `.marim/` (this repo does, `.gitignore:37`), so the warning
is the exception rather than the norm — which is exactly why it is worth
printing when it does apply.

## Failure handling

`memory.py`'s contract is "never raise into a turn" — it logs and returns a
falsy value. This is a CLI, not a turn, so failures are loud:

- `save_memory` returning `None` (unwritable dir, etc.) is reported per-slug on
  stderr and makes the command exit `1`.
- An unreadable or non-UTF-8 source file is reported and skipped; it does not
  abort the run. Other memories still import.
- A source dir that resolves but is empty is a clean exit `0` with a "nothing to
  import" line.

## Tests

`tests/test_claude_import.py`:

- `claude_project_slug` — plain path, dotted dir (`marim.dev` → `marim-dev`),
  leading-dot dir (`/.local` → `--local`), trailing slash.
- `claude_config_dir` — honors `CLAUDE_CONFIG_DIR`, falls back to `~/.claude`.
- `parse_memory_file` — well-formed; missing `description`; unknown
  `metadata.type`; no frontmatter at all (→ `None`); body containing `---`.
- Title recovery — from the index; orphan file falls back to its slug; an index
  line whose title contains `](` does not misattribute (guards the shared regex).
- `plan_import` — fresh import; existing slug skipped; existing slug with
  `force` → overwrite.
- End-to-end against a tmp fake Claude config dir and tmp workspace: files land
  with correct frontmatter, target `MEMORY.md` gains one entry per import, and a
  **dry run writes nothing** (assert the target dir does not exist afterward).
- A failing `save_memory` (monkeypatched to return `None`) yields exit `1` and a
  stderr mention of the slug.

`tests/test_cli_import.py` in the style of `test_cli_trust.py`: argparse
surface, source-miss listing + non-zero exit, `--from` pointing at both a
project dir and a memory dir, and the privacy warning firing only on `--apply`
in a non-ignored git repo.

## Docs

- `docs/guides/` page for the command.
- ROADMAP: mark the memory slice of `marim import claude` as landed, keep the
  remaining pieces (hooks, skills, MCP, `CLAUDE.md`) listed.
- CHANGELOG entry.

## Deliberately not in scope

- Importing skills, sub-agents, hooks, MCP servers, or `~/.claude/CLAUDE.md`.
- A `--scope global` target.
- Preserving Claude's `originSessionId` / `modified` provenance.
- Reverse export (marim → Claude).
- Importing Claude *session transcripts* (a different subsystem entirely).
