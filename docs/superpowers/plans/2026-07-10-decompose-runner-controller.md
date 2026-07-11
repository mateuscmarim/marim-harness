# Decompose SubagentRunner and _run_with_approval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Behavior-preserving decomposition of the two units flagged by the codebase
review as violating the project's own complexity guidelines: `subagents/runner.py`
(1281 lines → extract a run-driver collaborator and a CLI-spawn collaborator) and
`runtime/controller.py:_run_with_approval` (~256 lines → extract failure handling
and the approval/success tails, leaving straight-line orchestration).

**Architecture:** Two new leaf-ish modules in `subagents/` (`run_driver.py`,
`cli_spawn.py`) holding cohesive collaborators the runner constructs in `__init__`
and delegates to; two/three new private methods plus a tiny enum inside
`controller.py` (no new file there — the extraction is within the class).
No public API changes; the shared `_run_spawn_lifecycle` invariant stays on the
runner and is passed to the CLI collaborator as a bound callable.

**Tech Stack:** Python ≥3.10, pydantic-ai, uv, pytest, ruff (line 100), pyright.

## Global Constraints

- **Behavior-preserving.** No semantic change anywhere: same classification
  order, same one-shot latch semantics, same teardown dispositions, same
  usage-banking points, same log/notice strings. If a change would "improve"
  behavior, it is out of scope — report it instead.
- **Invariant comments move verbatim with their code.** This codebase treats
  why-comments as load-bearing (CLAUDE.md). Never drop or paraphrase one during
  a move; adjust only self-references that name the old location.
- Branch: `refactor/decompose-runner-controller`, created from
  `fix/review-findings` (commit `03e385e`). Line anchors below refer to that
  commit; later tasks in the same file must re-locate by method name.
- Run only the named targeted suites per task
  (`uv run pytest --no-cov -p no:cacheprovider <files>`); the full
  `ruff → pyright → pytest` gate runs once in Task 5.
- `uv` for everything. Ruff line length 100. No 3.11+-only syntax.
- Tasks 1→2 are sequential (same file), tasks 3→4 are sequential (same file);
  the (1,2) chain and the (3,4) chain are independent of each other.
- Commit at the end of each task with the message given in the task.

---

### Task 1: Extract `_handle_run_failure` from `_run_with_approval`

**Files:**
- Modify: `src/marim_harness/runtime/controller.py` (method `_run_with_approval`,
  lines ~583–735 at the anchor commit; the `except BaseException` block is
  lines ~632–735)
- Test (existing, must stay green): `tests/test_turn_controller.py`,
  `tests/test_provider_errors.py`, `tests/test_recovery.py`,
  `tests/test_steering.py`, `tests/test_session_sanitize.py`,
  `tests/test_agent.py`, `tests/test_approval.py`

**Interfaces:**
- Consumes: existing `TurnController` fields/methods only
  (`self.session`, `self._reclaim_undelivered_steers`, `self._contention_backoff`,
  `self._maybe_compact`, `self._flush_resumable`, `self._pending_error_note`,
  `_actionable_error_note`, `dump_provider_error`, `is_context_overflow_error`,
  `overflow_is_contention`, `estimate_tokens`,
  `ContextWindowExceededError`, `CONTEXT_CONTENTION_HELP`, `CONTEXT_OVERFLOW_HELP`).
- Produces (Task 2 and the loop rely on these exact names):
  - `class _RunRetry(Enum)` with members `CONTENTION` and `COMPACTED`
    (module level, near the other private helpers).
  - `async def _handle_run_failure(self, exc, captured, resumable,
    deferred_results, round_usage, retried) -> _RunRetry` — returns a retry
    directive after handling it (backoff already slept / compaction already
    done+persisted), or **raises** (the converted overflow error, or `exc`)
    when the round is not retryable. Mutates `retried` by adding the returned
    member, so each kind fires at most once per turn.

- [ ] **Step 1: Read the method and its tests first**

Read `_run_with_approval` in full, plus `tests/test_provider_errors.py` and
`tests/test_recovery.py` to see what the existing tests pin (they call
`run_turn`/`_run_with_approval` behaviorally — none monkeypatch inside the
except block, so no test repointing is expected; verify that with
`grep -n "_handle_run_failure\|overflow_retried\|contention_retried" tests/ -r`
(expect no hits).

- [ ] **Step 2: Add the enum and the new method**

At module level in `controller.py` (imports: add `from enum import Enum, auto`):

```python
class _RunRetry(Enum):
    """How a failed ``agent.run`` round may be retried. Members double as the
    one-shot latch keys in the ``retried`` set ``_run_with_approval`` threads
    through ``_handle_run_failure`` — each kind gets exactly one shot per turn
    (a contention retry doesn't consume the compaction retry, and vice versa)."""

    CONTENTION = auto()  # pool contention: retry in place after a backoff
    COMPACTED = auto()   # genuine overflow: history force-compacted; retry
```

New method on `TurnController`, placed directly above `_run_with_approval`.
Its body is the **verbatim move** of the current `except BaseException` block's
contents (lines ~634–735), with exactly these mechanical substitutions:

```python
async def _handle_run_failure(
    self,
    exc: BaseException,
    captured: list[ModelMessage],
    resumable: list[ModelMessage],
    deferred_results: DeferredToolResults | None,
    round_usage: RunUsage,
    retried: set[_RunRetry],
) -> _RunRetry:
    """Resolve one failed ``agent.run`` round: bank its spend, reclaim
    undelivered steers, then either hand back a one-shot retry directive
    (contention → backoff already slept; overflow → history already
    force-compacted and persisted) or flush a resumable history, stash the
    error note, spill the provider payload, and re-raise. ``retried`` is the
    cross-round latch set: a directive is only returned if its kind isn't
    already in it, and the returned kind is added here so the caller can't
    forget to latch it."""
```

Substitutions (everything else, including every comment block, moves verbatim):
- `contention_retried` → `_RunRetry.CONTENTION in retried`
- `overflow_retried` → `_RunRetry.COMPACTED in retried`
- the two `continue` statements become:
  ```python
  retried.add(_RunRetry.CONTENTION)
  return _RunRetry.CONTENTION
  ```
  and (after the successful `_maybe_compact(force=True)`):
  ```python
  retried.add(_RunRetry.COMPACTED)
  return _RunRetry.COMPACTED
  ```
  (the `resumable = list(self.session.history)` refresh does NOT move — the
  caller does it, see Step 3, because `resumable` is loop state).
- the final bare `raise` becomes `raise exc` (we are no longer lexically inside
  the `except` block; `exc.__traceback__` is preserved, and Python's
  self-context guard makes `raise exc` safe here). The
  `raise ContextWindowExceededError(...) from exc` line is unchanged.

- [ ] **Step 3: Rewrite the loop's except block as a delegation**

In `_run_with_approval`, delete the two boolean latches and their comments
(lines ~594–602) and replace with:

```python
        # One-shot retry latches, one per _RunRetry kind — see _RunRetry and
        # _handle_run_failure for why each recovery path gets exactly one shot
        # and why they don't consume each other.
        retried: set[_RunRetry] = set()
```

Replace the whole `except BaseException as exc:` body with:

```python
                except BaseException as exc:
                    retry = await self._handle_run_failure(
                        exc, captured, resumable, deferred_results, round_usage, retried
                    )
                    if retry is _RunRetry.COMPACTED:
                        # The compacted-and-persisted history is the new
                        # rollback baseline for the retry (maybe_compact
                        # persisted it).
                        resumable = list(self.session.history)
                    continue
```

- [ ] **Step 4: Run the targeted suites**

Run: `uv run pytest --no-cov -p no:cacheprovider tests/test_turn_controller.py tests/test_provider_errors.py tests/test_recovery.py tests/test_steering.py tests/test_session_sanitize.py tests/test_agent.py tests/test_approval.py`
Expected: all pass (same counts as before the change — record the before count
first with `git stash` if unsure).

- [ ] **Step 5: Lint and type-check the file**

Run: `uv run ruff check src/marim_harness/runtime/controller.py && uv run pyright`
Expected: clean / 0 errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/runtime/controller.py
git commit -m "refactor(runtime): extract _handle_run_failure from _run_with_approval"
```

---

### Task 2: Extract `_resolve_approval_round` and `_finish_turn`

**Files:**
- Modify: `src/marim_harness/runtime/controller.py` (`_run_with_approval` only;
  after Task 1 the DeferredToolRequests branch and success tail are the
  remaining bulk)
- Test (existing, must stay green): same suite list as Task 1.

**Interfaces:**
- Consumes: Task 1's shape of `_run_with_approval`.
- Produces:
  - `async def _resolve_approval_round(self, requests: DeferredToolRequests,
    resumable: list[ModelMessage]) -> DeferredToolResults` — fires the
    approval-needed notification (ask mode), resolves approvals, and on ANY
    failure rolls history back to `resumable`, best-effort persists, and
    re-raises.
  - `async def _finish_turn(self, output: str) -> str` — fires the Stop hook
    (never lets it crash the completed turn) and schedules autoname; returns
    `output`.

- [ ] **Step 1: Extract `_resolve_approval_round`**

Move the body of the `if isinstance(result.output, DeferredToolRequests):`
branch — from the `if self.deps.workspace.mode is Mode.ask ...` notification
block through the `try/except BaseException` around `resolve_approvals`
(current lines ~769–808) — verbatim into:

```python
    async def _resolve_approval_round(
        self, requests: DeferredToolRequests, resumable: list[ModelMessage]
    ) -> DeferredToolResults:
        """One approval round: notify (ask mode), resolve against the current
        Mode, and on any failure roll back to the last cleanly persisted
        baseline before re-raising — the in-memory history at this point ends
        with unanswered tool calls and must never be what a resumed session
        sees. See _run_with_approval for why the dirty history is never
        persisted during the round."""
```

Substitutions: `result.output` → `requests` (three occurrences: the
`.approvals` gate, the names join, and the `resolve_approvals(...)` call);
`return`s the value that was assigned to `deferred_results`. Every comment
moves verbatim.

The branch in `_run_with_approval` becomes:

```python
            if isinstance(result.output, DeferredToolRequests):
                # This history ends with unanswered tool calls; keep it in memory
                # for the continuation run but do NOT persist it. A cancel or
                # failure during approval would otherwise leave the session
                # ending in a dangling tool_use — unresumable. Roll back to the
                # last clean state if the approval round is interrupted.
                deferred_results = await self._resolve_approval_round(
                    result.output, resumable
                )
                user_prompt = None  # continuation is driven by deferred_results
                continue
```

- [ ] **Step 2: Extract `_finish_turn`**

Move the tail — `output = result.output`, the Stop-hook try/except, and
`schedule_autoname` + `return` (current lines ~826–839) — into:

```python
    async def _finish_turn(self, output: str) -> str:
        """The post-persist turn tail: Stop hook (must never crash a completed
        turn) and the non-blocking autoname schedule. Returns the final text."""
```

The end of `_run_with_approval` becomes:

```python
            await asyncio.to_thread(self.session.persist)
            # This round completed cleanly and is persisted — it becomes the new
            # rollback baseline for any subsequent round.
            resumable = list(self.session.history)
            # Compact after the turn completes so the gauge never shows >100%
            # for long: the mid-turn growth is folded in immediately rather
            # than waiting for the next turn's start-of-turn check.
            await self._maybe_compact()
            return await self._finish_turn(result.output)
```

(The persist-offload comment above `asyncio.to_thread(self.session.persist)`
stays where it is.)

- [ ] **Step 3: Sanity-check the residual method size**

`_run_with_approval` should now be roughly 60–90 lines including comments,
with no nesting deeper than the single `try/except` around `agent.run`.
If anything else still bulks it, leave it — scope is these two extractions.

- [ ] **Step 4: Run the targeted suites**

Run: `uv run pytest --no-cov -p no:cacheprovider tests/test_turn_controller.py tests/test_provider_errors.py tests/test_recovery.py tests/test_steering.py tests/test_session_sanitize.py tests/test_agent.py tests/test_approval.py`
Expected: all pass, same counts.

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/marim_harness/runtime/controller.py && uv run pyright`
Expected: clean / 0 errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/runtime/controller.py
git commit -m "refactor(runtime): extract approval-round and finish-turn tails from _run_with_approval"
```

---

### Task 3: Extract `SpawnRunDriver` into `subagents/run_driver.py`

**Files:**
- Create: `src/marim_harness/subagents/run_driver.py`
- Modify: `src/marim_harness/subagents/runner.py`
- Modify (repoint white-box tests): `tests/test_subagent_retry.py`,
  `tests/test_subagent_depth.py`, `tests/test_subagent_masking.py`,
  `tests/test_context_limits.py`, `tests/test_provider_errors.py`,
  `tests/test_subagent_resume.py`
- Test (behavioral, must stay green): `tests/test_subagent_retry.py`,
  `tests/test_subagent_depth.py`, `tests/test_subagent_masking.py`,
  `tests/test_subagent_concurrency.py`, `tests/test_subagent_safety.py`,
  `tests/test_subagent_timing.py`, `tests/test_context_limits.py`,
  `tests/test_detach_fanout.py`

**Interfaces:**
- Consumes: `RetryPolicy` (`subagents/policies.py`), `Deps`
  (`runtime/deps.py`), `SessionController` (`session/ctrl.py`), the error
  classifiers from `runtime/errors.py`, `mask_stale_observations`
  (`subagents/masking.py`).
- Produces (runner and tests rely on these exact names):
  - `class SpawnRunDriver` with:
    - `__init__(self, deps: Deps, session: SessionController,
      retry: RetryPolicy, known_window: Callable[[], int | None]) -> None`
    - `async run_to_completion(self, sub, task, run_deps, granted, handler,
      stream_id=None, history=None) -> AgentRunResult[str]` — signature and
      body identical to today's `SubagentRunner._run_to_completion`.
    - `async backoff(self, attempt: int) -> None` — the test-stub seam
      (today's `_retry_backoff`; keep its "kept as a thin method so a test can
      stub it" docstring, updated to name `SpawnRunDriver.backoff`).
    - private: `_shed_context`, `_notice_retry`, `_notice_overflow`, and the
      class constants `_SHED_KEEP_RECENT = 1`, `_SHED_MIN_CHARS = 64`.
  - Module-level moves into `run_driver.py`: `_fresh_capture` and
    `_resumable_history` (runner re-imports `_resumable_history` — it still
    uses it in `resume_spawn`).
  - `SubagentRunner` gains `self._driver: SpawnRunDriver` built in `__init__`:
    `SpawnRunDriver(deps, session, self._retry, self._known_window)`.

- [ ] **Step 1: Audit every consumer before moving**

```bash
grep -rn "_run_to_completion\|_retry_backoff\|_shed_context\|_notice_retry\|_notice_overflow\|_fresh_capture\|_resumable_history\|_SHED_" src tests
```
Every `src` hit must be inside `runner.py` today (if not, stop and report).
Record the `tests` hits — they are the repoint list for Step 4.

- [ ] **Step 2: Create `run_driver.py`**

Module docstring: one paragraph — "Drives a built sub-agent's model loop to
completion: transient-error retry with resume, context-overflow shed,
pool-contention classification, and the foreground UI notices. Extracted from
SubagentRunner so the runner stays the spawn-lifecycle coordinator." Then move,
verbatim including all comments and docstrings:
- `_fresh_capture` (with its contextmanager import needs),
- `_resumable_history`,
- the driver class as specified in Interfaces, whose method bodies are today's
  `_run_to_completion` / `_retry_backoff` / `_shed_context` / `_notice_retry` /
  `_notice_overflow` with only these substitutions:
  - `self._retry_backoff(` → `self.backoff(`
  - `self._known_window()` stays — it is now the injected callable:
    store as `self._known_window = known_window` and call identically.
  - `self.deps` / `self.session` reads are unchanged (same attribute names on
    the driver).
Imports: copy exactly the subset runner.py uses for these bodies
(`RunUsage`, `UsageLimits`, `AgentRunResult`, `capture_run_messages` internals
used by `_fresh_capture`, `is_context_overflow_error`,
`is_transient_model_error`, `overflow_is_contention`, `estimate_tokens`,
`last_request_input_tokens`, `mask_stale_observations`, `RetryPolicy`, `Deps`,
logging). Run `uv run ruff check` on the new file to catch unused/missing ones.

- [ ] **Step 3: Rewire `runner.py`**

- In `__init__`, after `self._masking = ...`:
  ```python
  # The model-loop driver: retry/overflow/contention recovery lives there,
  # keeping this class the spawn-lifecycle coordinator. known_window is
  # passed as a callable because it reads the *current* session model.
  self._driver = SpawnRunDriver(deps, session, self._retry,
                                self._known_window)
  ```
- `_execute_native_spawn`'s `_run` closure: `self._run_to_completion(` →
  `self._driver.run_to_completion(`.
- `resume_spawn`: `_resumable_history` now comes from
  `from .run_driver import SpawnRunDriver, _resumable_history`.
- Delete from `runner.py`: `_fresh_capture`, `_resumable_history`,
  `_run_to_completion`, `_retry_backoff`, `_shed_context`, `_notice_retry`,
  `_notice_overflow`, the two `_SHED_*` constants, and any imports that ruff
  now flags unused.

- [ ] **Step 4: Repoint the white-box tests**

Mechanical, assertions unchanged:
- `runner._run_to_completion(` → `runner._driver.run_to_completion(` (also the
  `h.subagents.` variants in `test_subagent_depth.py`).
- `runner._retry_backoff = X` → `runner._driver.backoff = X`.
- Spy save/restore lines (`orig = runner._run_to_completion`) follow the same
  rename. Use the Step 1 grep output as the checklist; re-run the grep after —
  zero hits on the old names outside `run_driver.py` itself.

- [ ] **Step 5: Run the targeted suites**

Run: `uv run pytest --no-cov -p no:cacheprovider tests/test_subagent_retry.py tests/test_subagent_depth.py tests/test_subagent_masking.py tests/test_subagent_concurrency.py tests/test_subagent_safety.py tests/test_subagent_timing.py tests/test_context_limits.py tests/test_provider_errors.py tests/test_subagent_resume.py tests/test_detach_fanout.py`
Expected: all pass, same counts.

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check src/marim_harness/subagents tests && uv run pyright`
Expected: clean / 0 errors.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/subagents/run_driver.py src/marim_harness/subagents/runner.py tests/
git commit -m "refactor(subagents): extract SpawnRunDriver from SubagentRunner"
```

---

### Task 4: Extract `CliSpawnOrchestrator` into `subagents/cli_spawn.py`

**Files:**
- Create: `src/marim_harness/subagents/cli_spawn.py`
- Modify: `src/marim_harness/subagents/runner.py`,
  `src/marim_harness/subagents/backend.py` (gains `CONTINUATION_PROMPT`)
- Modify (repoint white-box tests): `tests/test_subagent_isolation.py`
  (`h.subagents._run_cli = ...` → `h.subagents._cli.run_cli = ...`), plus any
  hits from the Step 1 grep.
- Test (behavioral, must stay green): `tests/test_subagent_cli_spawn.py`,
  `tests/test_cli_backend.py`, `tests/test_cli_demux.py`,
  `tests/test_subagent_resume.py`, `tests/test_subagent_isolation.py`,
  `tests/test_spawn_transcripts.py`

**Interfaces:**
- Consumes: Task 3's runner shape (`self._driver` exists; `_run_spawn_lifecycle`
  still on the runner).
- Produces:
  - In `backend.py`: `CONTINUATION_PROMPT: str` — today's
    `SubagentRunner._CONTINUATION_PROMPT` text, moved verbatim (module-level,
    with a one-line comment: shared by native resume in the runner and CLI
    resume in cli_spawn, and living here because backend.py is the leaf both
    import).
  - `class CliSpawnOrchestrator` with:
    - `__init__(self, deps: Deps, hooks: TurnHooks,
      transcripts: SpawnTranscripts,
      lifecycle: Callable[..., Awaitable[str]],
      resolve_agent: Callable[[str], AgentDef | None]) -> None`
      (`lifecycle` is the runner's **bound** `_run_spawn_lifecycle`;
      `resolve_agent` the runner's bound `_resolve_agent`)
    - `async execute(self, defn, task, work_root, iso, mcp_names,
      max_output_chars, model, stream_id, *, background,
      resume_session_id=None, original_task=None, depth=1,
      transcript_prefix=None) -> str` — body of today's `_execute_cli_spawn`.
    - `async run_cli(self, defn, task, work_root, model, stream_id,
      checkpoint=None, resume_session_id=None) -> CliResult` — body of today's
      `_run_cli` (keep the lazy `from .cli_backend import ...` inside it).
    - `async resume(self, stream_id: str, meta: dict) -> tuple[str | None, str]`
      — body of today's `_resume_cli_spawn`.
    - `@staticmethod _mcp_note(mcp_names) -> str` — today's `_cli_mcp_note`.
  - `SubagentRunner` gains `self._cli: CliSpawnOrchestrator` built in
    `__init__` after `self._transcripts`.

- [ ] **Step 1: Audit consumers**

```bash
grep -rn "_execute_cli_spawn\|_run_cli\b\|_resume_cli_spawn\|_cli_mcp_note\|_CONTINUATION_PROMPT" src tests
```
All `src` hits must be in `runner.py`. Record `tests` hits for Step 4.

- [ ] **Step 2: Move `CONTINUATION_PROMPT` to `backend.py`**

Add at module level in `backend.py` (verbatim text from the runner):

```python
# The resume prompt every interrupted spawn continues from — shared by the
# native resume path (runner.resume_spawn) and the CLI resume path
# (cli_spawn.CliSpawnOrchestrator.resume); it lives on this leaf module so
# neither importer needs the other.
CONTINUATION_PROMPT = (
    "You were interrupted before finishing. The conversation above is your "
    "own earlier progress on this task — continue from where it leaves off "
    "and finish the task, then report as usual."
)
```

Delete `_CONTINUATION_PROMPT` from the runner; both call sites import
`CONTINUATION_PROMPT` from `.backend`.

- [ ] **Step 3: Create `cli_spawn.py` and rewire `runner.py`**

Module docstring: "Runs and resumes `backend: claude-cli` spawns — the external
`claude -p` process path. Owns the CLI-side meta/checkpoint templates and the
`--resume` relaunch; rejoins the runner's shared `_run_spawn_lifecycle` (passed
in as `lifecycle`) so run+failure+finalize stays written once." Bodies move
verbatim with only these substitutions:
- `self._run_spawn_lifecycle(` → `self._lifecycle(`
- `self._run_cli(` → `self.run_cli(`
- `self._cli_mcp_note(` → `self._mcp_note(`
- `self._resolve_agent(` → `self._resolve_agent(` (same name, now the injected
  callable stored in `__init__`)
- `self._transcripts` / `self.deps` / `self.hooks` unchanged (same attribute
  names on the orchestrator).
- `self._CONTINUATION_PROMPT` → `CONTINUATION_PROMPT` (imported from `.backend`).
- `resume` calls `self.execute(...)` where `_resume_cli_spawn` called
  `self._execute_cli_spawn(...)`.

In `runner.py` `__init__`, after `self._transcripts = ...`:

```python
# The claude-cli spawn path: external-process execute/resume live there;
# it rejoins this runner's _run_spawn_lifecycle (passed bound) so the
# run+failure+finalize invariants stay written once.
self._cli = CliSpawnOrchestrator(
    deps=deps, hooks=hooks, transcripts=self._transcripts,
    lifecycle=self._run_spawn_lifecycle, resolve_agent=self._resolve_agent,
)
```

- `_execute_spawn`'s CLI branch → `return await self._cli.execute(defn, task,
  work_root, iso, mcp_names, max_output_chars, model, stream_id,
  background=background, depth=depth)`.
- `resume_spawn`'s CLI branch → `return await self._cli.resume(stream_id, meta)`.
- Delete `_execute_cli_spawn`, `_run_cli`, `_resume_cli_spawn`,
  `_cli_mcp_note` from the runner; prune now-unused imports (ruff will flag).
- Verify no import cycle: `cli_spawn.py` must not import `runner`
  (`uv run python -c "import marim_harness.subagents.runner"`).

- [ ] **Step 4: Repoint white-box tests**

From the Step 1 grep: `h.subagents._run_cli = X` →
`h.subagents._cli.run_cli = X` (test_subagent_isolation.py:391), and any other
hits analogously. Assertions unchanged. Re-grep: zero hits on old names.

- [ ] **Step 5: Run the targeted suites**

Run: `uv run pytest --no-cov -p no:cacheprovider tests/test_subagent_cli_spawn.py tests/test_cli_backend.py tests/test_cli_demux.py tests/test_subagent_resume.py tests/test_subagent_isolation.py tests/test_spawn_transcripts.py tests/test_subagent_depth.py`
Expected: all pass, same counts.

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check src/marim_harness/subagents tests && uv run pyright`
Expected: clean / 0 errors.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/subagents/ tests/
git commit -m "refactor(subagents): extract CliSpawnOrchestrator from SubagentRunner"
```

---

### Task 5: Docs, full gates, and size check

**Files:**
- Modify: `CLAUDE.md` (the `subagents/` bullet under "Supporting subsystems")

**Interfaces:** none — documentation and verification only.

- [ ] **Step 1: Update CLAUDE.md's subagents bullet**

Extend the existing bullet to name the new modules, keeping its style:
`runner.py` (spawn lifecycle coordinator), `run_driver.py` (model-loop
retry/overflow/contention recovery), `cli_spawn.py` (the `claude -p`
execute/resume orchestration), alongside the existing `masking.py` /
`cli_backend.py` mentions.

- [ ] **Step 2: Size sanity check (informational)**

Run: `wc -l src/marim_harness/subagents/runner.py src/marim_harness/subagents/run_driver.py src/marim_harness/subagents/cli_spawn.py`
Expected: runner.py ≲ 900; report the numbers in the task report.

- [ ] **Step 3: Full CI-parity gates**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest -q -p no:cacheprovider`
Expected: ruff clean; pyright 0 errors; full suite green with coverage ≥ 90%.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: note run_driver/cli_spawn split in the subagents map"
```
