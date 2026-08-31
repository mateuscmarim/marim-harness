# Claim Hygiene — Closing the Five Phase-4 Carry-overs

**Date:** 2026-08-31
**Status:** design, approved in brainstorming (user-ruled)
**Builds on:** phase 2 session-ownership claims (PR #106, merged as `02ebcf4b`),
spec `2026-08-29-cross-process-session-events-design.md` §4-5.

## Problem

Phase 2 landed single-owner claims for the three entry points (TUI launch,
headless run, serve daemon). Its own plan deferred five residuals ("## Deferred
to phase 4", `docs/superpowers/plans/2026-08-29-session-ownership-claim.md`),
and the SDD review wave recorded the user's rulings on them
(`.superpowers/sdd/2026-08-29-session-ownership-claim/progress.md` lines 16-17).
Live probe B during phase-2 verification reproduced the largest one: a TUI
`/sessions` switch onto a daemon-claimed session ran **unclaimed**, and the
launch session stayed falsely claimed after the switch.

The five items:

1. **In-TUI switch seam.** `switch_to_session_id`
   (`interfaces/tui/session_view.py:687-693`) → `Harness.switch_session`
   (`runtime/harness.py:744`) reloads the store with no claim involvement.
   The TUI can drive a session owned by another process, and keeps claiming
   the session it left.
2. **runtime.json write-before-bind.** `serve.py` writes `runtime.json`
   (line ~357 area) before uvicorn binds (line 435); a doomed second daemon
   overwrites the live daemon's record, and its pid-guarded `clear_runtime`
   still removes the record it wrote — discovery goes blank while the live
   daemon runs.
3. **Claim-after-build read race.** `default_cmd.py` builds the harness
   (lines 219/250 — which reads the session store) **then** claims via
   `_run_claimed`; the read happens before ownership is established.
4. **Delete resurrection window.** `SessionManager.delete`
   (`session/store.py:533`) removes a claimed session's file and sidecar; a
   racing persist between unlink steps can resurrect the session claimless.
5. **Docstring overstatement.** `claim.py:1` promises "two processes never run
   one harness", which the switch gap (item 1) and the accepted
   degrade-to-unclaimed stance contradict.

## Goals

- A session is claimed exactly while a process actively drives it; switching
  views moves the claim, and switching onto a claimed session is refused with
  the holder's identity.
- A doomed daemon never disturbs the live daemon's `runtime.json`.
- Ownership is established before any session read in the CLI paths.
- Deletion refuses claimed sessions and orders file-before-sidecar.
- The claim module documents what it actually guarantees.

## Non-goals

- The degrade-to-unclaimed stance on claim-file open failure stays as-is —
  the user's merge of PR #106 accepted it (`file_lock` precedent). Do not
  re-litigate absent a real incident.
- No phase-3/4 remote-attach machinery (WebSocket `Subscription`,
  `RemoteSessionHost`, session picker).
- No compat flag, no `MARIM_*` switch for any of this.

## Decisions (brainstorming, user-ruled)

- **Packaging:** one branch, all five items (`fix/claim-hygiene`).
- **Claim lifecycle:** the claim follows the **active view**, not the process
  lifetime. Successful switch releases the outgoing claim; switch/new onto a
  claimed target is refused.
- **Swap location:** Harness-level (one seam covers `/sessions`, the picker,
  and `/new`), not duplicated in the TUI layer.
- **Bind order:** pre-bind the listen socket, publish `runtime.json` only
  after a successful bind, hand the fd to uvicorn.

## Design

### Item 1 — claim follows the active view

**Exception relocation.** `SessionClaimed` moves from `server/supervisor.py`
to `session/claim.py` (it describes a claim condition, not a server concept);
`supervisor.py` imports it from there. Its fields (`.session_id`, `.holder`)
and HTTP mapping (`http.py` → 409 `claimed`) are unchanged.

**Harness owns the claim.** `default_cmd` keeps acquiring the launch claim
(its refusal UX stays), then hands it to the Harness via
`Harness.adopt_claim(claim)` — the Harness stores it as `self._claim`.
Release is idempotent via `Harness.release_claim()`; the TUI exit path and
`default_cmd`'s `finally` call it. Invariants preserved: identity JSON is
still written to the locked fd only (`claim.py` unchanged here), the sidecar
is still `<id>.json.claim`, and the daemon-side release funnel
(`SessionHost.aclose`) is untouched.

**Switch.** `Harness.switch_session(session_id)`:

1. Resolve the target store path; `try_acquire` it. If held → raise
   `SessionClaimed` **before** touching the outgoing session (the existing
   job-history snapshot/restore path at harness.py:744-770 means a refused
   switch is a no-op).
2. Load via `self.session.switch_session`. If the load raises, release the
   tentative claim and re-raise (outgoing claim survives — same no-op
   guarantee as the job-snapshot logic).
3. On success: release the outgoing claim, install the target claim.

The ordering claim-before-load matters: a refused switch must not have wiped
the outgoing job context (it hasn't — refusal happens first), and a failed
load must not strand a claim on a session we never reached.

**New session.** `Harness.new_session(name)`: release the outgoing claim,
then `try_acquire` the fresh id (the sidecar is creatable before the session
file exists; `claim_path` derives from the store path). Acquisition of a
brand-new unique id cannot realistically race; on the impossible held case,
degrade with a warning log and proceed unclaimed (consistent with the
accepted stance).

**`/clear`** (reset) stays on the same session — claim untouched.

**TUI refusal UX.** `switch_to_session_id` and the picker dispatch catch
`SessionClaimed` and mount a `NoticeMessage` naming the holder
(`holder.describe()` — e.g. "daemon (pid 2963517) at http://127.0.0.1:8643")
instead of switching. The TUI stays on its current session.

### Item 2 — bind first, publish second

`serve.py` gains a small seam, `bind_listener(host, port) -> socket`
(stdlib `SO_REUSEADDR`, `.bind((host, port))`):

1. Bind. `OSError` (port taken) → exit with the "already in use" message
   **before** any `runtime.json` write. This is the fix: a doomed second
   daemon can no longer overwrite or clear the live record.
2. `write_runtime(...)` with the fd-bound endpoint (`format_base_url` —
   already IPv6-safe from `964481d4`).
3. `uvicorn.run(app, fd=sock.fileno(), ...)` — uvicorn adopts the socket;
   the socket object stays alive until `run` returns (do not close it first).
4. `clear_runtime` stays in the shutdown `finally`, pid-guarded as shipped.

`SessionSupervisor(endpoint=...)` keeps receiving the same string (the fd
handoff changes nothing about the advertised URL).

### Item 3 — claim before build

`default_cmd` resolves the target session id **before** `build_harness`:

- explicit `--resume <id>` → that id;
- otherwise a small pure resolver `resolve_target_session(store_dir) ->
  str | None` picking the most recent session for the workspace (the same
  latest-session rule the bootstrap resume path uses — extracted, not
  re-implemented; if bootstrap has no single helper, mirror its list+sort and
  unit-test the resolver against it).

Then: `try_acquire` → refuse-with-exit-2 as today if held → `build_harness(...,
resume=resolved_id)` → `adopt_claim`. Build failure releases the claim in
`finally`. Both paths (headless at line ~219, TUI at ~250) move the claim
ahead of the build; `_run_claimed` is reshaped accordingly (claim is an input,
not something it creates post-build).

The residual list→claim micro-window (another process claims between our list
and our `try_acquire`) stays documented, not eliminated — `try_acquire` is the
atomic arbiter and the window cannot produce two owners, only a refused start.

### Item 4 — delete refuses claimed sessions

`SessionManager.delete` (`session/store.py:533`) is the shared seam: the CLI
(`interfaces/cli/sessions.py:94`) and the HTTP DELETE route
(`server/http.py:452`) both go through it.

1. First: `read_holder` on the session. Claim held → raise `SessionClaimed`.
   Callers convert: HTTP → 409 `claimed` (reuse the `post_message` mapping
   pattern); CLI `_cmd_delete` → exit 2 with the holder named.
2. Deletion order becomes session file **then** claim sidecar (sidecar last),
   so a racing persist cannot find a claimless file it should have been
   blocked by. With refuse-while-claimed and unique timestamp ids, the
   resurrection window closes; the remaining micro-race (claim appears
   between our check and the unlink) gets one documented line — `try_acquire`
   cannot succeed while a holder exists, so the race cannot produce a
   claimless live session under normal operation.

### Item 5 — docstring honesty

`claim.py:1` reworded from "so two processes never run one harness" to claims
that track the session a process is **actively driving** — with a sentence
noting the accepted degrade-to-unclaimed stance on claim-file open failure
(mirrors `atomic_io.file_lock`). No behavioral change.

## Docs

- `docs/reference/serve-api.md` line 516 ("that lands in phase 4") rewritten:
  in-TUI switches now swap claims; switching onto a claimed session is
  refused.
- The 409 bullet gains the delete-route mapping.

## Testing

- Item 1: swap success (old released / new held, verified with `flock -n`
  semantics via `try_acquire` probes), refused switch leaves both untouched
  and raises with holder identity, failed load releases the tentative claim,
  `new_session` swaps, idempotent `release_claim`.
- Item 2: `bind_listener` contention (second bind on the same port raises),
  runtime.json never written when bind fails.
- Item 3: resolver purity (latest-session rule), claim-before-build ordering
  (a held session is refused before `build_harness` runs — assert no store
  read/seeding occurred, e.g. via mtime or a counting stub).
- Item 4: delete of a claimed session raises/refuses in both callers; file-
  then-sidecar ordering.
- Item 5: none (prose).
- All phase-2 claim tests continue to pass unchanged (three invariants
  intact).

## Gates

`uv run ruff check src tests` · `uv run ruff format --check src tests` ·
`uv run pyright` · `uv run pytest` (default and Python 3.10). C901 cap 10, no
`# noqa: C901`; line length 100; no 3.11+-only syntax.

## Out of scope

Phase 3/4 remote attach (WebSocket `Subscription`, `RemoteSessionHost`,
daemon sessions in the picker); the degrade stance itself; serve-api beyond
the two statements above.
