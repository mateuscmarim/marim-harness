# Codebase Review — marim-harness (excluding `interfaces/`)

**Date:** 2026-07-03
**Snapshot:** HEAD `3ef6c46` (working tree clean except this report)
**Method:** 9 parallel subsystem audits, each reading its scope in full and probing adversarial cases.
**Scope:** everything under `src/marim_harness/` except `interfaces/` (~19k source lines).

## Overall grade: **7.5 / 10**

LOC-weighted, the subsystem grades average ~8.0, but the overall is docked to **7.5**
because a single **critical** security finding in `config/` re-opens exactly the
RCE/exfil vectors the rest of the codebase works hard to close — and it fires from
merely cloning a hostile repo. Fix that one issue and this is a solid 8.0 codebase.

## Scorecard

| Subsystem | LOC | Grade | Headline |
|---|--:|:--:|---|
| `runtime/` | 2666 | **8.5** | Resumability invariants genuinely upheld at every persist site |
| `mcp/` + `lsp/` | 1821 | **8.5** | Disciplined lifecycle; tests target the exact hard cases |
| top-level modules | 2094 | **8.5** | `atomic_io` durability textbook-correct; classifiers fail closed |
| `tools/` | 3041 | **8.0** | Rigorous SSRF/output-flood defense; one `tree` symlink escape |
| `subagents/` | 2522 | **8.0** | Belt-and-suspenders depth ceiling; CLI cancel-path divergence |
| `workspace/` | 1895 | **8.0** | Sound path sandboxing; memory-index newline injection |
| `session/` | 1534 | **8.0** | Checkpoint/rewind-undo model sound; concurrency gaps |
| `plugins/` + `hooks/` | 1413 | **8.0** | Trust gates enforced on executable surface; inert-text gap |
| **`config/`** | 1986 | **6.0** | **Critical `.env` blocklist bypass** |

## 🔴 Critical — fix first

**`config/env.py` — project `.env` can redirect the "trusted" global config.**
`XDG_CONFIG_HOME` / `XDG_DATA_HOME` are not in `_PROJECT_ENV_BLOCKLIST`. When
`XDG_CONFIG_HOME` is unset (the common Linux/macOS case), `load_environment` does
`os.environ.setdefault` for every non-blocked project key, then loads the "trusted"
global config from `Path(XDG_CONFIG_HOME)/marim/.env` — which is *allowed to set every
blocklisted key*. A cloned repo whose `.env` sets `XDG_CONFIG_HOME=.evil` makes
`<repo>/.evil/marim/.env` the trusted config, which can then set
`MARIM_PROVIDER=claude-cli` + `MARIM_CLAUDE_CLI_BIN=./evil.sh` (RCE) or
`MARIM_BASE_URL` / `OPENROUTER_API_KEY` (exfil) — self-contained in the clone.

- **Fix:** add `XDG_CONFIG_HOME` / `XDG_DATA_HOME` to `_PROJECT_ENV_BLOCKLIST`
  (or snapshot the config dir before applying project values).
- **Test gap:** every existing blocklist test sets `XDG_CONFIG_HOME` via `monkeypatch`,
  so the unset path — the precise condition under which the bypass fires — is untested.

## 🟠 Notable mediums (security-adjacent)

- **`tools/fs.py:316` `_walk_tree`** — `tree` follows symlinks out of the workspace
  (verified: `link_out → /outside` gets fully enumerated). Lone outlier; every sibling
  read tool guards this. Discloses names outside the sandbox to an ungated sub-agent tool.
- **`plugins/discovery.py:221` + `install.py:300`** — project-scope plugin
  **skills / sub-agent specs / AGENTS.md** load with **no `trust_project` gate**
  (hooks/MCP *are* gated), so a hostile clone auto-injects plugin instructions into
  `@agent.instructions`; and `update_plugin` retains trust across upstream updates that
  *change* existing hook/MCP commands (trust-once ⇒ trust-all-future-upstream).
- **`workspace/memory.py:96`** — model-controlled `description`/`hook` embedded into
  `MEMORY.md` and YAML frontmatter without newline sanitization; a `\n` writes an orphan
  line that silently defeats the upsert dedup and accumulates in the always-injected index.
- **`runtime/context.py:138` `actionable_error_note`** — an OpenRouter-400-wrapping-
  transient error is misclassified as an actionable client error (branches on surface
  status, never unwraps the body like `errors.py` does), telling the model to truncate
  after an infra blip.
- **`subagents/runner.py:960`** — a cancelled *fresh isolated* `claude-cli` spawn uses
  `discard()` (drops its branch), yet its sidecar is stamped `running` and offered for
  resume, which then refuses — the exact resume-breakage the native path avoids.
- **`session/store.py:50` + `checkpoints.py:140`** — unnamed parallel sessions collide on
  a second-resolution id (no PID/random suffix), and the checkpoint sidecar writes without
  `file_lock` — two processes on the same session last-writer-wins, silently dropping
  checkpoints.

## Per-subsystem findings

### `runtime/` — 8.5
- `[MEDIUM] context.py:138` — `actionable_error_note` misclassifies a 400-wrapped transient as actionable (see above).
- `[LOW] controller.py:788 vs 807` — turn-start `_maybe_compact()` runs before drop/repair sanitize; a process kill in that window leaves a compacted-yet-dirty file. Swap the order.
- `[LOW] controller.py:131` — `_drop_nameless_tool_calls` re-parses every string-typed tool-arg on every request across the whole history.
- `[LOW/design] controller.py:566-778` — `_run_with_approval` is one ~210-line method; the overflow-recovery and failure-flush blocks are cohesive extractions.

### `mcp/` + `lsp/` — 8.5
- `[LOW] mcp/manager.py:259` — `mcp_status` can under-report live servers when a cancelled server precedes a successful one in config order.
- `[LOW] mcp/manager.py:300` — `disable_server` never tears down the live subprocess; the stdio child runs all session.
- `[LOW] lsp/manager.py:374` — `workspace_symbols` doesn't apply the dead-server eviction the hot path does.
- `[LOW] lsp/manager.py:238` — evicted-then-restarted servers leave stale exit callbacks on the session `_stack`.

### top-level modules — 8.5
- `[LOW] read_only_commands.py:64` — a whitelisted `git` subcommand can execute a shell via a `!`-alias in a hostile gitconfig (outside stated threat model).
- `[LOW] atomic_io.py:99` — `_sweep_stale_temps` glob-escapes the directory but not `name`; a filename with glob metacharacters (`foo[1].json`) never matches its own temps, so crash-leftovers accumulate.
- `[LOW] atomic_io.py:119` — `os.replace` silently resets file mode to `0o600` on every rewrite (undocumented for future mode-sensitive callers).
- `[LOW] usage.py:116` — `provider:model` slugs discard the provider hint (only `provider/model` is parsed), reducing pricing accuracy.

### `tools/` — 8.0
- `[MEDIUM] fs.py:316` — `tree` follows symlinks out of the workspace (see above).
- `[MEDIUM/design] provider.py` — 1163-line god-module hosting ~20 unrelated tool concerns.
- `[LOW] web.py:93` — a malformed/non-dict SearXNG result element raises an uncaught `AttributeError`/`TypeError` and fails the turn.
- `[LOW] fs.py:144` — full-file scan per paginated read; a giant single line breaks the "bounded by window" comment.

### `subagents/` — 8.0
- `[MEDIUM] runner.py:960` — CLI-spawn cancellation uses `discard()`, diverging from the native `close()` cancel path and breaking resume (see above).
- `[LOW] runner.py` — 1262-line runner, 18 ctor params; the CLI lifecycle duplicates the native one and is a candidate collaborator extraction.
- `[LOW] cli_backend.py:518` — `asyncio.get_event_loop()` inside a running coroutine (deprecated; CI runs 3.14).
- `[LOW] runner.py` — cancel arms never fire `subagent_stop` after `subagent_start`; `_slot()` cap doesn't bound worktree/hook setup.

### `workspace/` — 8.0
- `[MEDIUM] memory.py:96` — model-controlled description/hook unsanitized into `MEMORY.md`/frontmatter (see above).
- `[LOW-MEDIUM] memory.py:127` — `save_memory` write path can raise `OSError` despite the "never raises into a turn" docstring; `remember` has no try/except.
- `[LOW] snapshot.py:145` — clean-tree fast path reuses cached `_last_commit` with no reachability check; interleaved delete+gc can lose a checkpoint.
- `[LOW] memory.py:46 / plans.py:15` — duplicated `_slugify` with divergent fallbacks.

### `session/` — 8.0
- `[MEDIUM] store.py:50` — unnamed parallel sessions collide on a second-resolution id (no PID/random suffix).
- `[MEDIUM] checkpoints.py:140` — `_save()` writes the checkpoint sidecar without `file_lock`; concurrent same-session processes last-writer-wins.
- `[LOW] store.py:167` — `save_meta` docstring claims it doesn't rewrite the messages array, but it fully re-parses + re-serializes it.
- `[LOW] checkpoints.py:197` — `_pre_restore`/`_pre_undo` refs outlive their usefulness (pin whole-tree snapshots until session delete); `_pre_undo_commit` has no consumer.

### `plugins/` + `hooks/` — 8.0
- `[MEDIUM] discovery.py:221` — project-scope plugin skills/agents/instructions load with no `trust_project` gate (see above).
- `[MEDIUM] install.py:300` — `update_plugin` retains trust across upstream updates that change existing hook/MCP commands.
- `[LOW] install.py:285` — `url = rec.source["url"]` raises unhandled `KeyError` for a malformed registry entry.
- `[LOW] discovery.py:315` — untrusted `server_name` not run through `valid_name` before building the namespaced key.

### `config/` — 6.0
- `[CRITICAL] env.py:28,119,122` — `XDG_CONFIG_HOME`/`XDG_DATA_HOME` not blocklisted → trusted-config redirection (see above).
- `[LOW] model.py:173` — dead defensive branch (`_int_env` already maps negatives to default).
- `[LOW] model.py:298` — `_int_env` rejects a legitimate `0` (e.g. `mask_keep_recent=0`).
- `[LOW] claude_cli_model.py:171 vs openrouter_cost.py:83` — inconsistent cost rounding (truncate vs round).

## Cross-cutting observations

- **Consistent engineering culture.** Every reviewer independently praised the same
  things: security decisions documented with the *concrete attack* they prevent,
  fail-closed/fail-soft defaults, argv-never-shell subprocess handling, and
  invariant-focused tests targeting the actual hard cases. Mature, security-conscious.
- **Two recurring smells.** (1) **God-modules** — `tools/provider.py` (1163 lines) and
  `subagents/runner.py` (1262 lines, 18 ctor params) both flag coding-guidelines
  cohesion/large-state rules. (2) **Overstated invariant comments** — several docstrings
  claim guarantees the code doesn't quite deliver (`save_meta` "doesn't rewrite messages";
  `memory` "never raises into a turn"; `atomic_io` "bounded by window"). Comments are
  load-bearing here, so drift is worth correcting.
- **Test coverage is a genuine strength** (~40k test LOC vs ~28k src) and the gaps map
  almost 1:1 to the findings — each finding comes with an obvious regression test to add.

## Recommended fix order

1. **Critical:** blocklist `XDG_CONFIG_HOME`/`XDG_DATA_HOME` (+ unset-XDG test).
2. **Mediums:** `tree` symlink guard; gate project-scope plugin skills/agents/instructions;
   memory-index newline clamp; `actionable_error_note` body-unwrap; CLI-spawn cancel →
   `close()` parity; session id PID suffix + checkpoint sidecar lock.
3. **Design/lows:** split the two god-modules; reconcile the overstated docstrings; the
   assorted low-severity edge cases per subsystem.
