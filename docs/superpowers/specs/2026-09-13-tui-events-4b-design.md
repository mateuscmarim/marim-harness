# Phase 4b — Jobs over the wire: an attached TUI sees and acts on the daemon's jobs

**Date:** 2026-09-13
**Status:** Draft for approval
**Parent spec:** `docs/superpowers/specs/2026-08-29-cross-process-session-events-design.md` (phase 4)
**Previous phase:** `docs/superpowers/specs/2026-09-13-tui-events-4a-design.md` (#119)
**Branch:** `feat/tui-events-4b`

## Context

After 4a a TUI attached to a daemon-owned session renders the transcript,
asks and status from the wire, and every process-local feature refuses with
one notice. Background jobs were deliberately left blind:

- `app.jobs` is an empty, process-local `JobRegistry` when attached, so the
  jobs panel is always empty and `jobs.changed` is delivered and ignored.
- `finish_replayed_cards` demotes every "running" spawn sidecar to
  "unknown" (`_unknown_running`) because this process cannot tell a spawn
  the daemon is still driving from one that died: the card stays pending
  forever, no orphan card is synthesized, and the `r` resume affordance is
  withheld.
- `/jobs` and the sub-agents screen's resume are refused as remote-only.

The daemon already serves `GET .../jobs` and `GET .../jobs/{id}` (the
`jobs_view` DTOs) but nothing acts on a job, and neither route is in the
API reference. 4b closes exactly that gap: **the daemon's registry is the
attached TUI's jobs source of truth, and the two verbs a user has on a job
(cancel, resume a spawn) get routes.** Everything else in the 4a non-goals
list (daemon-hosted by default, in-place switching, the wider command
surface, takeover) stays deferred.

## Decisions

**`GET jobs` is the authority; `jobs.changed` is the trigger.** The event
keeps its empty payload (it is published from the registry's synchronous
`on_change`, where assembling DTOs would mean sidecar I/O in a callback).
An attached TUI re-reads `GET jobs` on every `jobs.changed`, coalesced: a
burst of changes restarts one exclusive refresh worker, and the last read
wins. Jobs change on launch and settle only, so this is a handful of small
requests per turn, not a stream.

Rejected: *carry the job rows on `jobs.changed`.* It would give the mirror
exact ordering with no round trip, but introduce a second job shape (rows
without the sidecar metrics) next to the DTO, and the attach path needs
the GET anyway.

**A read-only mirror, not a registry.** The attached app holds a
`JobMirror` — the daemon's rows as `Job` dataclasses (no task, no kill, no
output function) behind the read surface every panel and settle path
already uses (`list()`, `history`, `get()`, `any_running()`,
`has_finished_pending()`). `app.jobs` is typed to that surface (`JobsView`
protocol); the local path keeps handing out the live `JobRegistry`
untouched. The mirror answers `has_finished_pending()` with `False` for the
same reason the attached `ActivityMonitor` is built with depth 0: the
daemon owns autonomous wake, and a second driver would race it.

**Actions are link commands.** `SessionLink` grows `jobs()`,
`job_output(id)`, `cancel_job(id)` and `resume_spawn(stream_id)`. Locally
they delegate to the registry and `services.resume_subagent`; remotely they
are `GET jobs`, `GET jobs/{id}`, `POST jobs/{id}/cancel` and
`POST subagents/{stream_id}/resume`. `/jobs list|output|cancel` and the
`r` key therefore run the same code on both kinds — the 4a "link command"
bin, no `if attached:` in the handlers. `/jobs wake` stays
`require_local`: the toggle is the daemon's (`autonomous_wake=True` is its
default), and there is no route to flip it.

**The daemon's truth decides replay.** With the mirror populated before
the transcript is replayed, `finish_replayed_cards` runs the in-process
logic unchanged: a "running" sidecar whose stream has a live job in the
mirror is re-armed onto that job (`adopt_resumed_card`, the same path a
fresh resume uses) and streams on; one with no live job really is
interrupted — the daemon is not running it — so it is flagged and
resumable, and an orphan (running sidecar, no card) is synthesized exactly
as in process. `_unknown_running` survives only as the degraded path: when
the jobs read failed at attach, the mirror is marked unsynced and the 4a
demotion applies, because "pending" is still the honest answer when this
process does not know.

**Full results on live settles, tails at attach.** The list DTO gains
`result_tail` (the same 200-char whitespace-collapsed tail the persisted
history stores; `null` while running), so a replayed detached card settles
with the same text a local resume shows, with no extra requests. When a
job settles while the TUI is attached and its card is still mapped, the
refresh fetches `GET jobs/{id}` for the full result before repainting —
the card then reads exactly as it does in process. Bounded by the number
of cards still waiting, idempotent (a fetched id is remembered), and safe
to interrupt: a restarted refresh recomputes the set from what is still
mapped.

**`GET jobs` on a cold host returns the persisted history.** Today it
returns `[]` whenever the session's host is not loaded, which reads as
"nothing ever ran". The session file carries the settled summaries
(`jobs` in the header, written before the messages array so the picker's
header fast path reads it without parsing the transcript). The route now
serves those rows, status `done|failed|cancelled` with their tails. For
the attached TUI this is the eviction-race fallback (a host evicted
between the claim probe and the jobs read); for other clients it is the
honest answer for an idle session.

## Goals

1. An attached TUI's jobs panel shows the daemon's jobs — running and
   settled — and repaints as they launch and finish.
2. Replay settles sub-agent cards from the daemon's registry: a spawn the
   daemon is still driving stays live in its card; one it is not running
   is flagged interrupted and can be resumed with `r`, which starts the
   resume on the daemon and re-arms the card onto the new job.
3. `/jobs`, `/jobs list`, `/jobs output <id>` and `/jobs cancel <id>` work
   attached; `/jobs wake` refuses with the usual notice.
4. Every failure is visible: a jobs read that fails at attach or on refresh
   posts an error and leaves the panel as it was; a cancel or resume the
   daemon did not take is reported with its reason.
5. The API reference documents the jobs routes (the follow-up owed since
   the mode-switch route landed) and the two new ones.
6. The local path is byte-identical for the user: same registry, same
   panels, same tests; the only local change is that `/jobs output|cancel`
   and `r` go through the link wrapper.

## Non-goals (still deferred)

- Daemon-hosted by default; workspace auto-register; in-place
  local↔remote switching; automatic takeover when the daemon dies.
- The remaining process-local commands (`/compact`, `/name`, `/new`,
  `/rewind`, `/think`, `/advisor`, `/mcp`, Settings, `/remember`, `/skill`,
  `/plugin`, `/worktree`, `/trust`, `!cmd`).
- The task checklist over the wire (`tasks.changed` still carries no
  payload; the panel stays empty attached).
- Desktop notifications for the daemon's job completions (the notifier
  lives on the harness's deps; an attached TUI has none).
- Toggling the daemon's autonomous wake from a client.
- Live output of a running bash job over the wire (`GET jobs/{id}` returns
  the daemon's `output()` — the live buffer — so `/jobs output` already
  shows it; the panel does not poll for it).

## Architecture

```
daemon                                   attached TUI
JobRegistry ──on_change──► jobs.changed ──► _handle_jobs_changed
   │                                            │ (attached) run_worker(exclusive)
   ├── GET  jobs            ◄────────────────── link.jobs() ──► JobMirror.apply(rows)
   ├── GET  jobs/{id}       ◄────────────────── link.job_output(id)   (settled + still mapped)
   ├── POST jobs/{id}/cancel◄────────────────── link.cancel_job(id)   (/jobs cancel)
   └── POST subagents/{sid}/resume ◄─────────── link.resume_spawn(sid) (r key)
                                                │
                                                └─► activity.on_jobs_changed()
                                                    (fill detached cards, repaint panel)
```

### Server: routes and DTO

- `GET .../jobs` — unchanged shape plus `result_tail` on every row
  (`null` while running). When no host is loaded, the rows come from the
  session file's persisted `jobs` history (via the header fast path, full
  parse as fallback) instead of `[]`.
- `GET .../jobs/{id}` — unchanged; `result_tail` rides along for symmetry.
- `POST .../jobs/{id}/cancel` → `200 {"ok": true, "message": "cancelled job-1"}`.
  `404 job_not_found` for an unknown id or a cold host (no live jobs);
  `409 already_settled` with the registry's message when the job is not
  running. No body.
- `POST .../subagents/{stream_id}/resume` → `201 {"job_id": "job-3"}`.
  Loads the host if cold, through `_host_for_response` (so `409 claimed`
  and the delete race map as for `POST messages`). A refusal from
  `resume_spawn` (no sidecar, already finished, already resuming, unreadable
  transcript, worktree gone) is `409 resume_refused` with the reason as
  `message`. No body.

Both mutating routes are auth-gated and 404 on an unknown workspace or
session like every sibling.

### Client: `RemoteSessionHost`

- `jobs()` → `GET jobs` → `list[Job]` (dataclass rows; `result` =
  `result_tail`). Non-200 → `RemoteUnavailable("jobs not readable: …")`.
- `job_output(id)` → `GET jobs/{id}` → the `result` string. 404 → the
  registry's own wording (`No job 'x'.`) so `/jobs output` reads the same
  on both kinds. Other non-200 → `RemoteUnavailable`.
- `cancel_job(id)` → the route's `message` on 200 and 409; `No job 'x'.`
  on 404; other → `RemoteUnavailable`.
- `resume_spawn(stream_id)` → `(job_id, "")` on 201, `(None, message)` on
  409 `resume_refused`; other → `RemoteUnavailable`.

`LocalSessionLink` mirrors each onto the registry (`history + list()`,
`output`, `cancel`) and `services.resume_subagent` (absent → `(None,
"sub-agent resume is not available in this session")`).

### TUI

- `jobs.py` gains a `JobsView` protocol (the read surface) and
  `interfaces/tui/remote_jobs.py` a `JobMirror` implementing it:
  `apply(rows)` replaces the snapshot (`list()` = every row in the daemon's
  order, `history` = `[]`, `get()` finds any row, `synced` flag), and
  `settled_needing_result(mapped_ids)` returns the settled ids whose full
  result has not been fetched yet.
- `HarnessApp.jobs: JobsView` — the registry locally, the mirror attached.
  `_handle_jobs_changed` calls `activity.on_jobs_changed()` locally and
  `refresh_remote_jobs()` attached: an exclusive worker (group
  `jobs-refresh`) that reads `link.jobs()`, applies, fetches full results
  for settled jobs whose detached cards are still mapped, then runs the
  same `activity.on_jobs_changed()`. A `HostClosed` posts
  `jobs not refreshed: …` and leaves the mirror as it was.
- `_attach_remote` and `_on_stream_gap` await one initial read before the
  transcript is mounted (a failure posts `jobs not readable: …` and leaves
  the mirror unsynced); `finish_replayed_cards` applies `_unknown_running`
  only when attached and unsynced. The settle join reads `settled` from
  `history + list()` (a superset locally that changes nothing there: a
  live settled job's card is filled by `note_detached_spawn` before the
  join runs) so the mirror's rows count on both the settle and the
  orphan-skip.
- `_cmd_jobs`: `list` renders `app.jobs.list()`; `output` and `cancel`
  await the link; `wake` calls `require_local("/jobs wake")`. A
  `HostClosed` from the link posts an `ErrorMessage` (same as the steer
  and answer paths).
- `SubagentsScreen._resume`: `await app.link.resume_spawn(card.stream_id)`;
  a refusal goes to the pane as today, a `HostClosed` too (the card stays
  interrupted); success adopts the card onto the job id. The daemon's own
  `jobs.changed` (the resume registered a job) brings the panel up to date.
- `on_unmount` cancels the *harness's* registry, not `app.jobs` (the
  mirror has nothing to cancel).

## Wire changes (all additive)

- `JobDto.result_tail: string | null`.
- `POST .../jobs/{id}/cancel`, `POST .../subagents/{stream_id}/resume`.
- `GET .../jobs` on a cold host: persisted history instead of `[]`.
- `jobs.changed` documented as "re-read `GET .../jobs`".
- `docs/reference/serve-api.md`: endpoint summary rows and a Jobs section
  (list, detail, cancel, resume, the DTO); the "Attaching a local TUI"
  paragraph drops `/jobs` and sub-agent resume from the refused list and
  says what an attached TUI does with jobs. `docs/guides/tui.md` likewise.

## Testing

1. **DTO / view (pure):** `result_tail` on list and detail rows; the
   persisted-history rows for a cold host.
2. **Routes (`test_server_http.py`):** cancel — running → 200 and the job
   settles `cancelled`; settled → 409; unknown → 404; cold → 404. Resume
   — refusal → 409 with the runner's message; success → 201 with the new
   job id and the job registered; cold host loads via the factory. Auth
   and unknown-session 404s on both.
3. **Client (`test_server_client.py`, `MockTransport`):** each new method's
   status mapping, plus the real-daemon pass listing jobs.
4. **Local link parity (`test_tui_link.py`):** `job_output`/`cancel_job`
   delegate to the registry; `resume_spawn` without the service refuses.
5. **Mirror (`test_remote_jobs.py`):** apply/list/get/any_running,
   `has_finished_pending` is always False, `settled_needing_result` is
   idempotent.
6. **Attached app (`test_app_remote.py`):** the panel shows the daemon's
   jobs at attach; a running sidecar with a live job stays live (adopted),
   one without is interrupted and an orphan is synthesized; an unsynced
   mirror keeps the 4a pending behaviour; `jobs.changed` refreshes, fetches
   the full result for a settled mapped card and fills it; a failed refresh
   posts the error and keeps the panel; `/jobs list|output|cancel` go
   through the link and `/jobs wake` refuses; `r` resumes through the link
   (adopts on success, pane error on refusal, error message on transport
   failure).
7. **Live smoke (manual, model per approval):** daemon-owned session runs
   a background spawn; `marim --session <id>` shows it in the panel and
   the card streaming; `/jobs cancel` settles it; a killed-mid-spawn
   session attached shows the interrupted card and `r` resumes it on the
   daemon.

Gates in CI order (`ruff check` → `ruff format --check` → `pyright` →
`pytest` ≥ 90%), the ratchet gate's complexity/bandit/coverage numbers not
worse, and 3.12 parity on the touched files.

## Risks

- **Ordering between the refresh and the card map.** A settle's
  `jobs.changed` can arrive before the replay mapped the card (attach
  mid-settle). Covered: `note_detached_spawn` fills from the mirror at
  map time, and the settle join fills from `settled` afterwards; the
  full-result fetch keys off "still mapped", so a card filled with the
  tail is simply not re-fetched.
- **Refresh cancellation.** `exclusive=True` restarts the worker mid-read;
  the mirror is replaced atomically after the read, and the fetch set is
  recomputed, so a cancelled pass never leaves a half-applied state.
- **`settled` widened locally.** Reading settled rows from `list()` as
  well as `history` is a strict superset in process; the only cards it can
  touch are pending ones with a settled live job, which `note_detached_spawn`
  has already finished. The 3b/4a replay tests are the regression guard.
- **Cold-host history read cost.** The header fast path is what the picker
  already pays per row; the full-parse fallback is only for a header over
  64 KiB, and the route is `max-age=30`.
