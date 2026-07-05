# Codebase Review — marim-harness

**Date:** 2026-07-03
**Overall grade: 8 / 10**

Six parallel subsystem audits plus objective checks. Snapshot at commit `8547050`.

## Objective baseline

| Check | Result |
|---|---|
| ruff (`src`, `tests`) | clean |
| pyright (standard mode) | 0 errors, 0 warnings |
| pytest | 2,345 tests, all pass |
| coverage | 93.2% (enforced 90% floor) |
| size | 26.4k source lines / 38.1k test lines (1.4:1) |

## Scorecard

| Area | Score | Verdict |
|---|---|---|
| Tests / CI / docs | 8.5 | Behavior-first suite, incident-derived regression tests, real invariant coverage |
| Session / persistence | 8 | Real atomicity (fsync, locks, unique temps); a few narrow loss windows |
| Tools / sub-agents | 8 | Single-choke-point containment, genuine SSRF engineering; plan-mode gate leaks |
| Interfaces (TUI/CLI) | 8 | Turn-race discipline verified against live Textual; sub-agent resume path broken |
| Supporting (MCP/LSP/hooks/plugins/config) | 8 | Leak-proofed lifecycles, coherent threat model; one trust-gate hole |
| Runtime core | 7 | The hardest problems solved and documented; overflow retry violates its own invariants |

## What's genuinely strong

- **The resumability invariant is a real, defended contract.** "A persisted history never
  ends with an unanswered tool call" is enforced at every write site, backed by a
  start-of-turn self-heal (`runtime/controller.py:697-707`) and an abort-path repair
  (`_repair_unanswered_tool_calls`, `_flush_resumable`), and pinned by regression tests
  whose docstrings narrate the actual incidents they reproduce (`tests/test_recovery.py`).
  Reviewers verified the subtle claims (e.g., that pydantic-ai history processors see
  copies) against the installed libraries — the load-bearing comments are true.
- **Security engineering beyond boilerplate.** Path containment resolves symlinks at one
  choke point (`workspace/fs.py:54-63`) and every fs tool routes through it; `fetch_url`
  does DNS-pinned connects to close the rebinding window, per-redirect re-validation, and
  streamed size caps (`tools/fetch.py`); the config loader blocklists exactly the env vars
  a hostile repo's `.env` could use to self-elevate (`config/env.py:51-62`); hook
  subprocesses get process-group SIGKILL and reaping (`hooks/runner.py:88-118`).
- **The test suite is the standout.** Only 15 of 159 test files touch `unittest.mock`;
  the dominant pattern drives the real harness with scripted `FunctionModel`/`TestModel`
  and asserts observable outcomes (file contents, persisted history shape, exit codes).
  The three riskiest invariants — resumability, approval gating, atomic persistence —
  each have both unit and end-to-end coverage. The suite is hermetic (isolated
  `XDG_CONFIG_HOME`, opt-in `live` marker for network tests).
- **Architecture matches its documentation.** One composition root (`runtime/bootstrap.py`),
  one honest documented cycle (`Deps`↔services, bound once in `build_services`), pure
  helpers split from I/O and tested directly, both front-ends sharing `build_harness`.
  Adversarial checks of documented claims (Textual binding precedence, worker-group
  semantics, `git diff --output` behavior) were verified live, not assumed.
- **Lifecycle hygiene.** MCP connect/teardown is race- and leak-proofed
  (`mcp/manager.py:236, 269-285`); LSP ops degrade to human-readable strings instead of
  raising; every disk touch on the turn path is off-thread; cancellation stays snappy via
  deadline-bounded flushes.

## Findings, ranked

### High

1. **Security: project-scope plugins bypass the trust gate.**
   `.marim/hooks.json` / `.marim/mcp.json` are gated behind `MARIM_TRUST_PROJECT_HOOKS`,
   but a cloned repo can commit `.marim/plugins/` with a `plugins.json` marking a plugin
   `enabled:true, trusted:true`, and its hooks/MCP servers execute on first launch with no
   prompt (`plugins/discovery.py:51-56, 252-280`; consumed unconditionally by
   `hooks/config.py:52` and `mcp/config.py:197`). A committed trust bit is
   attacker-controlled — this walks around the gate the sibling files enforce.
   *Fix direction:* gate project-scope plugins behind the same trust flag, or ignore the
   on-disk `trusted` bit for the project scope and require interactive re-confirmation.

2. **Correctness: the context-overflow retry corrupts checkpoint rewind.**
   `runtime/controller.py:543-550` calls `session.maybe_compact(force=True)` directly,
   bypassing the `_maybe_compact` wrapper and therefore
   `checkpoints.invalidate_after_compaction()` (called from exactly one place,
   `controller.py:315`). After a forced mid-turn compaction, every checkpoint's absolute
   `history_len` points into the pre-compaction history; a later `/rewind` slices at a
   wrong boundary (`session/checkpoints.py:270`) and persists it — exactly the corruption
   the invalidation docstring warns about. **Found independently by two reviewers.**
   The same path also:
   - persists dirty approval-round history when the overflow hits a continuation round
     (violating the documented "dirty history is never persisted" invariant, and poisoning
     the rollback baseline at `controller.py:549`);
   - leaves `pre_turn_len` stale (`controller.py:672, 740`), skewing
     `_turn_produced_response` and the dead-checkpoint discard decision.

### Medium

3. **Plan mode's "read-only" bash gate admits real mutations.**
   `read_only_commands.py:38-57` checks only the git subcommand name, so
   `git branch <new>`, `git branch -D`, `git tag`, `git remote add`, and — verified
   live — `git diff --output=<file>` all auto-approve in plan mode
   (`runtime/permissions.py:39-41`). Cheap fix: argument/flag checks for
   `branch`/`tag`/`remote`, deny `--output`/`-o`.

4. **Narrow data-loss window on Ctrl-C.**
   `_flush_resumable`'s 0.25s `wait_for` abandons but cannot stop the
   `to_thread(persist)` worker (`controller.py:330-333`); if the disk stalls, the orphaned
   write later clobbers a newer persist and then sets `_last_persisted_version` from the
   *current* version (`session/ctrl.py:206-220`), so the next persist is cache-skipped
   while the disk holds stale data. The one path where an acknowledged-persisted turn can
   silently vanish.

5. **Sub-agent resume is broken in the TUI.**
   `replay_history` builds resumed sub-agent cards but never registers them with
   `stream.subagents` (`interfaces/tui/session_view.py:84-96` vs
   `stream_render.py:700-701`), so after resume, ctrl+x reports "No sub-agents spawned
   yet" and the persisted-transcript replay path is unreachable. Where reachable, the
   lazy transcript loader runs in the default worker group and is cancelled by any turn
   start with its `transcript_loaded` guard already set — truncated with no retry
   (`subagents_viewer.py:121-125`); the codebase documents this exact hazard elsewhere
   (`app.py:934-944`).

6. **Model-facing surface drift (tools/sub-agents).**
   - `spawn_agent`'s `max_depth` is exposed in the advertised tool schema and
     model-overridable (`tools/provider.py:687-690, 1095`); the leaf-depth tool-absence
     backstop holds, but the runtime guard is bypassable — close over it instead.
   - Sub-agents get a `bash` docstring recommending `job_output`/`wait_for_job`/`cancel_job`
     tools they don't have (`provider.py:876-881` vs `names.py:29`), so a background job
     spawned by a sub-agent is unretrievable by it and its completion digest lands on the
     main agent.
   - In ask/plan mode, sub-agent reach is silently stripped while the index still says
     "Full toolset" (`workspace/agents.py:257-263`) — orchestrator is never told.

7. **Runtime failure-path gaps.**
   - ~30-line unprotected window in `run_turn` between prompt assembly and the `try`
     block (`controller.py:680-713`): a raise there (e.g. flaky MCP in `toolsets_for`)
     permanently loses one-shot hook context / jobs digest and leaks a dead checkpoint.
   - The approval-rollback persist is unguarded inside `except BaseException`
     (`controller.py:626`): a persist `OSError` replaces an in-flight `CancelledError`.
   - `SessionStore.load()` misses `ValidationError` (`session/store.py:165-166`), so a
     version-skewed session file crashes resume with a raw traceback instead of the
     actionable `SessionLoadError`.
   - `set_model` does `persist(force=True)` mid-turn (`session/ctrl.py:227-230`),
     violating the dirty-history rule its sibling `rename` was rewritten to respect.

8. **Session deletion leaks.**
   `SessionManager.delete` unlinks only the session JSON (`session/store.py:291-292`),
   leaking the checkpoints sidecar, sub-agent transcript dir (`TranscriptStore.delete_all`
   has zero callers), image cache, and — notably — `refs/marim/checkpoints/<id>/*`, which
   pin whole-working-tree snapshot commits (including untracked files, potentially
   secrets) in `.git` indefinitely.

### Low / housekeeping

- No `py.typed` marker despite a pyright-clean package; wheel metadata is near-empty
  (no `readme`/`classifiers`/`urls`), no release automation or `uv build` check in CI.
- `.env.example` omits the `google` provider and ~25 of the 36 `MARIM_*` env vars the
  code reads (`MARIM_TRUST_PROJECT_HOOKS`, command allow/deny lists, `MARIM_LSP`, …).
- Docs staleness: README references the deleted `agent.py` (README.md:46, 231) and omits
  `runtime/`/`subagents/`/`hooks/`/`plugins/`/`lsp/` from its layout; CLAUDE.md says CI is
  "3.10 and 3.12" (actual: 3.10/3.12/3.14) and pyright "basic" (actual: standard).
- Stale invariant comments: `WorkspaceConfig` "never mutated" (`runtime/deps.py:105`,
  but `mode` is mutated); "spawn_agent is never among them, so sub-agents can't recurse"
  (`provider.py:1112-1116`, contradicted by the nested-spawn feature).
- Plan-mode/steer paper cuts: idle steer bypasses slash routing and history recall
  (`app.py:875-889`); `--mode plan` without `-p` on a tty silently ignored
  (`default_cmd.py:48-50`); one-way quit-warning latch discards queued messages silently
  after the first warning; `/model` applies mid-turn with no busy guard unlike its
  sibling commands.
- `is_context_overflow_error`'s `openai.APIError` branch has no status gate
  (`runtime/errors.py:231-240`) — a 429/5xx mentioning "context window" in prose
  classifies as overflow.
- A steer flushed into a round that fails before the next request boundary is silently
  lost (`controller.py:430-439, 583`).
- LSP: Python diagnostics run ruff/pyright even when the Python LSP is disabled
  (`lsp/manager.py:350-351`); `checks.python_diagnostics` resolves the ruff binary then
  invokes the bare literal (`lsp/checks.py:153-156`).
- `SessionStore.list()` fully parses every multi-MB session file for picker rows despite
  a `message_count` header existing to avoid it (`store.py:202-229`).
- Small pockets of dead code in the sub-agent viewer (`SubAgentPane.placeholder`,
  `_DETACHED_NOTE`, `SubAgentList.selected_index`).
- No `pytest-timeout`: a hung Pilot test stalls a CI leg instead of failing fast;
  the 90% coverage gate in `addopts` makes single-file runs fail unless `--no-cov`.
- Compaction can't shrink a single enormous turn (`compaction.py:96-109`): the overflow
  retry then fails permanently until manual `/clear`; `mask_stale_observations` — the
  lever that would help — only runs when compaction succeeded.

## Why 8

The mean of the subscores is ~7.9, and that matches the holistic picture. The core loop,
persistence, and tooling show discipline rarely seen at this scale — invariants that are
documented, enforced, tested, *and* verified true — and every gap found degrades to a
self-healed or recoverable state rather than a permanent wedge.

What separates this from a 9 is a pattern worth naming: **the newest features (overflow
retry, project plugins, sub-agent resume) were bolted on without threading through the
invariants the older code works hard to uphold.** Fixing the two HIGHs plus the plan-mode
git classifier and the flush-thread race — roughly two days of work, all with obvious
homes in the existing test suite — would honestly move this to a 9.
