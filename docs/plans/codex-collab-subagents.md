# Codex collab sub-agents as first-class cards

> Historical plan: the Codex CLI executor was removed on 2026-09-18.
> Codex execution now uses native `openai-codex` subscription access.

Spec for the next PR on the `codex-cli` provider. Drafted 2026-09-13 against
codex-cli 0.154.0 (app-server protocol v2, schema regenerated locally with
`codex app-server generate-json-schema`) and marim master `b2f32beb`. It is
the Codex counterpart of the claude-cli demux that PR #113 shipped
(`subagents/cli_demux.py`), and slots into the CLI backend parity roadmap
as a phase of its own (see §Docs → Roadmap placement; the roadmap is
[cli-backend-parity-roadmap.md](cli-backend-parity-roadmap.md), Phase 2b).

## Problem

Codex can spawn its own sub-agents (the *collab* tools: `spawnAgent`,
`sendInput`, `wait`, `closeAgent`, …). Each spawned agent is a **separate
app-server thread** whose `parentThreadId` is the session's thread. Today
marim sees only the parent's items, so a Codex-side sub-agent shows up as:

1. **A flat `codex_agent` tool card**, blank until the collab item
   completes (`translate._TOOL_NAMES` maps both `collabAgentToolCall` and
   `subAgentActivity` to `codex_agent`; the card's output is the item's
   `result`, which for `spawnAgent`/`wait` is a status blob, not the child's
   work). The marim-mobile screenshot that prompted this shows exactly that:
   `{"agentPath": "/root/review_task7", "kind": "interacted"}`, empty
   Output, "Working… 1m 8s".
2. **The child thread's notifications dropped.** `CodexServer._on_notification`
   routes by `threadId`; a thread it never registered is dropped at DEBUG
   (`codex notification %r for unknown thread %s dropped`) — deliberately,
   so a deregistered spawn's trailing events never leak into other sessions.
   The child's prose, reasoning and tool calls never reach any renderer.
3. **`subAgentActivity` pings rendered as separate same-named tool calls**
   (one `codex_agent` card per `started`/`interacted`/`completed` ping).
4. **Child approvals not brokered.** `_on_server_request` answers
   `RpcError(-32601, "no thread for …")` for an unregistered thread, so a
   child that asks for a command/file approval or user input under `ask`
   mode gets an error instead of the approval panel — a hole in the mode
   contract, not just a rendering gap. (Whether Codex escalates a child's
   approvals through the parent thread instead is the one wire fact this
   spec asks to verify live, §Ground truth.)
5. **Nothing persisted** for the child's work: the ledger (#124) records the
   `codex_agent` call/result pair only.

`codex_agent` is also undocumented anywhere in `docs/`.

## Goal

Under the `codex-cli` main-loop provider **and** for `backend: codex-cli`
spawns, a Codex-spawned agent renders and behaves like a native `spawn_agent`
child: a card in the sub-agents screen keyed to the spawn, a live transcript
(prose, thinking, tool cards), a model badge and live usage, collab
follow-ups (`sendInput`, `wait`, `closeAgent`) and activity pings as notices
on that card, the child's approval and user-input requests brokered through
marim's approval panel / `ask_user` under the session's mode, the spawn
persisted into history as a `spawn_agent` call/return pair, and the same
events riding the `marim serve` wire so an attached TUI and marim-mobile
see it too. Nothing in the parent's `ModelResponse` changes.

## Ground truth (wire, codex-cli 0.154.0)

Verified from the regenerated v2 schema (definitions unchanged since the
2026-09-09 dump):

- **`ThreadItem.collabAgentToolCall`** — required: `id`, `tool`
  (`CollabAgentTool`: `spawnAgent | sendInput | resumeAgent | wait |
  closeAgent | sendMessage | followupTask | interruptAgent | listAgents`),
  `status` (`inProgress | completed | failed | interrupted`),
  `senderThreadId`, `receiverThreadIds` (for `spawnAgent`: the newly
  spawned thread), `agentsStates` (`{threadId: {status, message?}}` with
  `CollabAgentStatus`: `pendingInit | running | interrupted | completed |
  errored | shutdown | notFound`). Optional: `prompt`, `model`,
  `reasoningEffort`. Arrives as `item/started` then `item/completed` on the
  **parent** thread.
- **`ThreadItem.subAgentActivity`** — required: `id`, `agentPath`,
  `agentThreadId`, `kind` (`started | interacted | interrupted |
  completed`). On the parent thread. Today's translator keeps `agentPath`
  and `kind` but drops `agentThreadId`.
- **`Thread`** carries `parentThreadId` (set only for sub-agents),
  `agentNickname`, `agentRole`. **`thread/started`** is a server notification
  with the full `Thread`; `SessionSource.subAgent.thread_spawn` carries
  `parent_thread_id`, `depth`, `agent_path`, `agent_nickname`, `agent_role`.
- Child item traffic, when delivered, uses the same notifications as any
  thread (`item/started`, `item/completed`, `item/agentMessage/delta`,
  `item/reasoning/*Delta`, `item/commandExecution/outputDelta`,
  `thread/tokenUsage/updated`, `turn/started`, `turn/completed`) with
  `threadId` = the child's id. `_THREAD_OBJ_METHODS`/`_thread_id_for`
  already extract it; the drop in `_on_notification` is the only thing in
  the way.
- **`thread/start.approvalsReviewer`** defaults to `user`; the server
  requests marim already brokers (`item/commandExecution/requestApproval`,
  `item/fileChange/requestApproval`, `item/permissions/requestApproval`,
  `item/tool/requestUserInput`, `mcpServer/elicitation/request`) carry
  `threadId` and are dispatched by `_on_server_request` through
  `_handle_for`.

**Verify with one live run before building on it (task 0):** run a
codex-cli session with `MARIM_DEBUG=1`, ask Codex to spawn an agent, and
grep the log for `for unknown thread`. Expected: child `item/*` and
`turn/*` notifications logged as dropped (the app-server fans a sub-agent's
events to the client that owns the parent). Also note whether a child's
`requestApproval` arrives with the child's `threadId` (→ brokered by the
adopted handle, §Design 1) or the parent's (→ nothing to do). If child
notifications do **not** arrive unsolicited, the fallback is to subscribe by
resuming the child id (`thread/resume` attaches a listener without starting
a turn) — the router below is unchanged either way; only the adoption call
in `CodexServer` differs.

**Task 0 result (2026-09-14, codex-cli 0.154.0, `multi_agent` stable/on,
`multi_agent_v2` off, headless `auto`):** child traffic DOES fan out to the
parent's client unsolicited — `turn/started`, `item/*` (`reasoning`,
`agentMessage` + deltas, `commandExecution`), `thread/tokenUsage/updated`,
`thread/status/changed`, `turn/completed`, all on the child's `threadId` —
and before adoption every one of them logged as
`codex notification 'item/started' for unknown thread 01a09db9-… dropped`
(42 lines for one two-sentence review). Two surprises against the schema
reading above: **no `thread/started` is sent for the child** (only for the
threads marim starts itself), and **no `spawnAgent` collab item exists** —
the spawn is reported as a `subAgentActivity` ping keyed by the tool-call id
(`{"type":"subAgentActivity","id":"call_…","kind":"started",
"agentThreadId":"01a09dbd-6068-…","agentPath":"/root/reviewer"}`), the
`collabAgentToolCall` that follows is a `wait` with `receiverThreadIds: []`
and `agentsStates: {}`, and completion is a second ping
(`"kind":"completed"`, id `subagent-completed-<turnId>`). The router
therefore opens the card on an unmapped `started` ping (`_spawn_from_ping`)
and settles it on the `completed` one. A child's approval **does
arrive under the child's `threadId`** (a second run had the agent write
outside the workspace: `item/commandExecution/requestApproval` with
`threadId` = its `agentThreadId`, `reason: "May I write hello to
$HOME/… outside the workspace as requested?"`), and the adopted handle
brokered it through the parent's `ApprovalBroker` (`auto` accepted, the
file appeared) — §Design 1 as built; before adoption the request had no
handle to reach.

## Design

Five pieces, mirroring the claude-cli demux where the shapes already
exist and reusing the codex spawn path's transcript adapter.

### 1. Child-thread adoption in `CodexServer` (`codex/server.py`)

- `ThreadHandle` gains `parent_id: str | None = None`.
- `CodexServer.adopt_thread(parent: ThreadHandle, child_id: str) ->
  ThreadHandle` registers a handle for the child that **shares the parent's
  `events` queue and `request_handler`**. Sharing the queue is the whole
  trick: the parent's `turn_events` loop already drains that queue, and
  every notification carries `threadId`, so child traffic interleaves with
  the parent's in arrival order with no second consumer task and no
  cross-task lifetime to manage. Sharing the request handler gives child
  approvals the parent's `ApprovalBroker` — same mode, same panel, same
  serialization lock (Codex can raise a child's and the parent's approval
  concurrently).
- `drop_thread` on a parent also drops its children (`_threads` scan by
  `parent_id`), so `aclose`, `_drop_own_thread` and a spawn's `finally:
  drop_thread` keep the invariant that a deregistered thread's trailing
  traffic never leaks. `thread_ids`/`idle` count children too (a live child
  pins the shared server open exactly like any other thread).
- `_on_notification` is unchanged: an adopted child is a registered thread.
  A notification for a thread that is *not* registered stays dropped — so a
  child that Codex reports through `thread/started` before the parent's
  `collabAgentToolCall` `item/started` lands is a race to handle in the
  router (below), not by broadcasting.
- `turn/completed` for a **child** must not end the parent's turn:
  `note_turn_completed` is per handle today and `_on_notification` calls it
  on the handle it resolved, so the child's completion updates the child
  handle only. `turn.py`'s `_is_stale_completion` compares turn ids and
  would already ignore it, but see §3 — the router filters child
  `turn/*` and usage before `_fold` ever sees them.

### 2. Translation (`codex/translate.py`)

New neutral items, keeping `ItemTranslator` pure:

```python
@dataclass(frozen=True)
class CollabCall:        # collabAgentToolCall item/started, on the parent
    item_id: str
    tool: str            # spawnAgent | sendInput | wait | closeAgent | ...
    receivers: tuple[str, ...]
    prompt: str | None
    model: str | None
    effort: str | None
    states: dict         # agentsStates as received

@dataclass(frozen=True)
class CollabDone:        # collabAgentToolCall item/completed
    item_id: str
    tool: str
    receivers: tuple[str, ...]
    status: str          # completed | failed | interrupted
    states: dict
    result: str          # _result_text(item)[0]
    is_error: bool

@dataclass(frozen=True)
class AgentPing:         # subAgentActivity (started or completed)
    item_id: str
    thread_id: str       # agentThreadId
    path: str            # agentPath
    kind: str            # started | interacted | interrupted | completed
```

`_TOOL_NAMES` loses both collab kinds; `_agent_activity_args` and the two
`_COMPLETED_BY_KIND` entries go with them. `_started`/`_completed` dispatch
the two kinds to the new items. Every field is optional on the way in
(version-tolerant: a missing `agentsStates` is `{}`, an unknown `tool`
passes through as its string).

Behavior without the router (a caller that ignores the new items — the
aux titler/summarizer clones run in plan mode and never spawn, so this is
defensive): `CollabCall`/`CollabDone`/`AgentPing` are simply not
`ActivityStart`/`ActivityEnd`, so `CodexStreamedResponse._events_for` and
`_Transcript.feed` ignore them and the parent's response is unaffected.

### 3. The router (`codex/collab.py`, new, pure)

`CollabRouter` sits between `turn_events` and the consumer, the codex analog
of `CliSubagentDemux.route`. It is constructed per parent thread (owned by
`CodexCliModel` / the spawn orchestrator, not per turn — children outlive a
turn, §Lifetimes) and is fed the raw `(method, params)` pairs the parent
loop pulls, *before* translation:

```python
@dataclass(frozen=True)
class Routed:
    stream_id: str | None   # None = the parent's own stream
    item: object            # a translate.* item, or a synthesized one
    usage: RunUsage | None  # child's accumulated usage snapshot (child only)
    model: str | None       # set once, on the child's first routed item

class CollabRouter:
    def __init__(self, parent_thread_id: str, *, adopt: Callable[[str], None],
                 release: Callable[[str], None]) -> None: ...
    def route(self, method: str, params: dict) -> list[Routed]: ...
    def seal_open(self) -> list[Routed]: ...   # parent turn ended
    @property
    def open_children(self) -> frozenset[str]: ...
```

Routing rules, keyed on `threadId`:

| Traffic | Handling |
| --- | --- |
| parent `collabAgentToolCall` **`spawnAgent`** started | for each receiver: `adopt(child)`, map `child → stream_id = item.id`, one `ItemTranslator` + one usage accumulator per child. Emit on the **parent** stream a synthesized `ActivityStart(item.id, "spawn_agent", {"type": nickname or role or "codex-agent", "task": prompt, "description": "", "model": model, "backend": "codex-cli", "thread_id": child})`. That single item is what makes the card: `activity_events` turns it into a `spawn_agent` `FunctionToolCallEvent`, the renderer's sinks build a card keyed by `tool_call_id == item.id`, and the ledger records the call. |
| parent `collabAgentToolCall` **other tools** started/completed | a **notice** on each receiver's card (`Routed(stream_id, Notice("sendInput: …"))`, text from `tool` + `prompt` or `result`), never a top-level card. `listAgents` and a call with no known receiver is dropped (logged at DEBUG). |
| parent `collabAgentToolCall` completed (any tool) | fold `agentsStates`: for each child whose status is terminal (`completed`, `errored`, `shutdown`, `interrupted`, `notFound`) → **settle** (below). For `spawnAgent` completed with `status == failed` and no child ever adopted → settle immediately with the error. |
| parent `subAgentActivity` | a notice on the card mapped from `agentThreadId` (`"agent /root/review_task7 interacted"`); `completed`/`interrupted` also settles if the child is still open. Never a card. |
| child `item/*`, `item/agentMessage/delta`, `item/reasoning/*Delta`, `item/commandExecution/outputDelta` | translate with the child's own `ItemTranslator`; emit on the child's stream. Nested `collabAgentToolCall` from a **child** (depth ≥ 2): the same rules apply recursively with the grandchild's stream id = that item id, and the synthesized `spawn_agent` start goes on the *child's* stream — the sub-agents screen already renders nested spawns as an indented tree. |
| child `thread/tokenUsage/updated` | accumulate into the child's `RunUsage` (`usage_from_total` of `total` minus the child's first-seen baseline, same seeding rule as `turn._seeded_baseline`); attach as `usage` on the next routed item; never reaches the parent's `TurnState`. |
| child `turn/started`, `turn/completed`, `error`, `warning` | consumed: a child turn completing is not the agent finishing (the parent may `sendInput` again). `turn/completed` with `status == failed` becomes a notice. |
| `thread/started` (parent id matches, child not yet mapped) | remember `Thread.agentNickname`/`agentRole`/`model` under the child id so the spawn card gets a name and model badge; if the `spawnAgent` `item/started` arrives *after* this, the stashed metadata is applied then. (Whether the app-server sends `thread/started` for sub-agents is part of task 0; the router works without it.) |
| anything else on the parent | passthrough as `Routed(None, <translated>)` — the existing path, untouched. |

**Settle** (child terminal): emit on the parent stream
`ActivityEnd(spawn_item_id, content, is_error)` where `content` is the
child's last agent-message text if any, else `agentsStates[child].message`,
else the collab result text; `is_error` for `errored`/`notFound`/`failed`.
Then `release(child)` (→ `server.drop_thread`) and forget the mapping. The
renderer settles the card on the `spawn_agent` `ToolReturnPart`, the ledger
records the result.

**Child model:** `Routed.model` is set once from `thread/started.thread.model`
or the `CollabCall.model`, formatted `codex-cli:<model>` like the spawn path
does (`codex_spawn.run_codex`).

### 4. Wiring the router (`config/codex_cli_model.py`, `codex/turn.py`, `subagents/codex_spawn.py`)

- `TurnState` gains `router: CollabRouter | None = None` (dataclass default —
  no new positional argument, ratchet-safe). `turn_events` calls
  `state.router.route(method, params)` when set and yields `Routed` items;
  when unset it yields the translated items exactly as today (the fake
  app-server tests stay green untouched). `_fold` sees only parent-stream
  items — child usage/turn notifications never reach it.
- `CodexCliModel` owns one router per thread handle (created in
  `_thread_for` beside the handle, torn down in `_drop_own_thread`) and
  passes it in `TurnState`. `CodexStreamedResponse` gains
  `_on_subagent`/`_on_subagent_model`/`_on_subagent_notice`/
  `_on_subagent_usage` and a `_deliver_routed` mirroring
  `claude_cli_model._deliver_routed`: `stream_id is None` → today's path
  (`_events_for` through the ledger-recording folder); else the child's
  translated item goes through a per-child transcript adapter (§5) and the
  resulting pydantic-ai events go to `on_subagent(stream_id, event, usage)`,
  with `on_subagent_model` first and `on_subagent_usage` on each usage
  change. `Notice` on a child stream → `on_subagent_notice(stream_id, text)`.
  In fold mode (no `on_activity`, headless) the synthesized `spawn_agent`
  start/end fold as `▸ spawn_agent <task>` / `▸ failed: …` lines through
  `fold_activity_text` unchanged; child items are dropped (headless has no
  card to stream into — same as claude-cli today).
- Non-stream `request()` (used by aux agents) ignores child streams: it
  appends only parent-stream text/activity, as now.
- `ExternalCliModel` grows `on_subagent_notice` and `on_subagent_usage`
  seams (attributes, no ctor args); `Harness.wire_cli_model` binds them to
  `deps.ui.on_subagent_notice` / `deps.ui.on_subagent_usage`. claude-cli
  can start using `on_subagent_usage` too (today it rides the event
  payload) — optional, out of scope.
- `codex_spawn.CodexSpawnOrchestrator.run_codex` gets the same router so a
  `backend: codex-cli` spawn whose Codex spawns grandchildren renders them
  nested under the spawn's card (the claude-cli spawn path already does this
  via `cli_backend`). The spawn's own `_Transcript` records the synthesized
  `spawn_agent` call/return into the sidecar so a resumed spawn replays the
  nested card; child transcripts are kept in `SpawnRun.child_transcripts`
  exactly like `cli_backend` does.

### 5. Child transcript adapter (`codex/transcript.py`, extracted)

`codex_spawn._Transcript` already folds `TextDelta`/`ThinkingDelta`/
`ActivityStart`/`ActivityEnd` into (a) pydantic-ai stream events for the
sub-agents screen and (b) a message list. Move it to `codex/transcript.py`
as `ItemTranscript` (public, unchanged behavior) and instantiate one per
child stream in the model and in the spawn orchestrator. No new rendering
code in the TUI: the events are the same shapes native and claude-cli
children produce.

### Lifetimes and turn boundaries

- Children **outlive a parent turn**: Codex keeps spawned agents alive
  across turns of the parent (`wait`/`sendInput` in a later turn), and a
  parent turn can end while a child is still running. Adopted handles and
  the router live on the model (per parent thread), not on `TurnState`.
- At the end of a parent turn (`turn_events` returns, or is interrupted),
  `router.seal_open()` emits, on the parent stream only, an `ActivityEnd`
  for every still-open spawn with `content = "running (detached; continues
  next turn)"`, `is_error=False`, flagged `ledger_only=True`. The model
  records those through `ledger.note_activity` **without** forwarding them
  to `on_activity`: the persisted response never carries an unmatched
  `spawn_agent` call (the expansion would otherwise synthesize an
  `interrupted` return), while the live card is not settled and keeps
  streaming when the child produces more in the next turn. A child that
  finishes in a later turn settles the card then; that turn's ledger gets a
  fresh `spawn_agent` call+result pair (`args.resumed = true`) so history
  stays self-consistent turn by turn.
- `interrupt` on the parent: Codex interrupts the whole agent tree; children
  report `interrupted` through `agentsStates`/`subAgentActivity` and settle
  normally. If they don't (the interrupt ack raced them), `_drop_own_thread`
  / `aclose` drop the children with the parent and the router's `seal_open`
  closes their cards as `interrupted`.
- **Resume** (`thread/resume` of a persisted parent): children of a prior
  process are gone; a `spawnAgent`-era card is not reconstructed (parity
  with claude-cli). The ledger-expanded history still shows the spawn call
  and its last sealed result.

### Approvals under `ask` and `plan`

A child's `item/*/requestApproval` / `item/tool/requestUserInput` /
elicitation resolves through `_handle_for` to the adopted handle → the
parent's `ApprovalBroker.handle`, so `decide` applies the same mode: `ask`
prompts per call, `auto` answers per Codex's own `on-request` policy, `plan`
never prompts (the child inherits the parent's `never` + read-only sandbox
— Codex propagates thread config to spawned agents; task 0 confirms). The
panel label gets an `agent <nickname|path>` prefix through
`ExternalRequest` so the user can tell whose command they are approving.
Denials flow back on the child's request id — no change to the broker.

### Serve parity

Nothing new on the wire: `on_subagent_event/notice/model/usage` already
publish `subagent.event`, `subagent.notice`, `subagent.model`,
`subagent.usage` (`server/host.py`), and the synthesized `spawn_agent`
call/result ride `tool.call`/`tool.result` like every other CLI tool card
(#122). marim-mobile renders a `spawn_agent` card + nested stream today for
claude-cli; codex-cli reuses it verbatim. `docs/reference/serve-api.md` gets
one sentence noting codex-cli now emits the sub-agent family.

### Persistence

The synthesized `spawn_agent` start/end go through
`ledger.recording(on_activity)` like any activity, so `expand_cli_activity`
turns them into a real `ToolCallPart`/`ToolReturnPart` pair in persisted
history (TUI replay, `GET history`, compaction, provider switch). Child
transcripts are **not** persisted for the main-loop provider (parity with
claude-cli; noted as a follow-up in the roadmap — a `provider_details`
sidecar keyed by stream id is the obvious shape). For `backend: codex-cli`
spawns they ride the sidecar as `child_transcripts` (see §4).

## Ratchet and complexity notes

- New modules (`codex/collab.py`, `codex/transcript.py`) start clean;
  `route` dispatches through a `{(scope, method): handler}` table to stay
  under C901 like `ItemTranslator._METHODS`.
- No function gains a 6th argument: the router is injected via a
  `TurnState` field, the seams are attributes on `ExternalCliModel`, and
  `adopt_thread(parent, child_id)` is two parameters.
- `CodexStreamedResponse` is a dataclass — new callback fields are fine
  (PLR0913 counts `def` parameters, not dataclass fields).
- Baseline check before pushing:
  `uv run ruff check --select C901,PLR0911,PLR0912,PLR0913,PLR0915 --no-cache src`
  (PLR0913 count must stay ≤ 60) and `docs/quality-gate.md` locally.

## Tests (offline, fake app-server)

- `tests/test_codex_translate.py`: `CollabCall`/`CollabDone`/`AgentPing`
  from schema-shaped fixtures; missing optional fields; unknown `tool`.
- `tests/test_codex_collab.py` (new): router unit tests — spawn → adopt +
  synthesized `spawn_agent` start on parent; child deltas routed with the
  child's translator; usage accumulation and baseline seeding; `sendInput`
  → notice; `subAgentActivity` → notice, `completed` settles; `agentsStates`
  terminal → settle + release; `spawnAgent` failed with no receiver;
  nested spawn from a child gets the child's stream id; `seal_open`;
  `thread/started` before/after `item/started` ordering.
- `tests/test_codex_server.py`: `adopt_thread` shares queue + handler;
  `drop_thread(parent)` drops children; a child `requestApproval` reaches
  the parent's handler; unknown threads still dropped.
- `tests/test_codex_cli_model.py` + `tests/fakes` app-server script: a full
  turn where the fake emits a parent `spawnAgent` item, child `item/*`
  traffic, a child approval request (ask mode → panel invoked with the
  agent label; plan mode → denied without prompting), child completion —
  asserting the `on_subagent_model`/`on_subagent`/`on_subagent_notice`/
  `on_subagent_usage` call sequence, that the parent `ModelResponse` is
  text-only, and that the persisted history (after
  `expand_cli_activity`) contains exactly one `spawn_agent` call+return.
  Plus: child open at turn end → sealed in the ledger, card not settled;
  next turn settles it.
- `tests/test_subagent_codex_spawn.py`: nested grandchild renders under the
  spawn's stream id; sidecar carries `child_transcripts`.
- `tests/test_codex_live.py` (opt-in, skipped in CI): one real spawn
  round-trip for task 0's evidence.

## Docs

- `docs/guides/subagents.md`: new section "Codex-side sub-agents" next to
  the Claude-side one — what a card shows, that collab follow-ups appear as
  notices, that approvals go through the panel, the resume caveat.
- The codex-cli section of `docs/guides/configuration.md` (or wherever
  the provider is described): replace the implicit `codex_agent` behavior
  with a pointer to the guide; document that `codex_agent` cards no longer
  exist.
- `docs/reference/serve-api.md`: codex-cli emits the `subagent.*` family.
- `CHANGELOG.md` under Unreleased: "codex-cli: Codex collab sub-agents are
  first-class cards…", and note the removal of the `codex_agent` card name
  (visible in persisted histories written by 0.7.x — those still expand
  fine; the name just stops being produced).
- Roadmap placement: add **"Phase 2b — Codex sub-agents"** to
  `docs/plans/cli-backend-parity-roadmap.md` between Phases 2 and 3 (it is
  independent of both and higher payoff than Phase 3), linking this spec,
  and list "persist child transcripts for the main-loop CLI providers" under
  *Later / exploring*.

## Task list (PR order)

0. Live probe (§Ground truth): record the log lines for child traffic and a
   child approval under `ask`; paste into the PR description.
1. `codex/server.py`: `ThreadHandle.parent_id`, `adopt_thread`, cascading
   `drop_thread`; tests.
2. `codex/translate.py`: the three collab items, drop `codex_agent`; tests.
3. `codex/transcript.py`: extract `ItemTranscript` from `codex_spawn`; tests
   move with it.
4. `codex/collab.py`: `CollabRouter`; tests.
5. `codex/turn.py`: `TurnState.router`, `turn_events` routing; tests.
6. `config/external_cli.py` + `runtime/harness.py`: the two new seams and
   their binding.
7. `config/codex_cli_model.py`: router ownership, `_deliver_routed`, seal at
   turn end, approval label; fake-server scenario tests.
8. `subagents/codex_spawn.py`: router in `run_codex`, nested cards, sidecar
   `child_transcripts`; tests.
9. Docs + CHANGELOG + roadmap phase.
10. Ratchet check, `ruff`/`format`/`pyright`/`pytest` in CI order; PR.

## Acceptance

- In a codex-cli TUI session, asking Codex to spawn an agent shows a
  `spawn_agent` card in the sub-agents screen within the same turn, with a
  streaming transcript, a `codex-cli:<model>` badge and live usage; collab
  follow-ups and `subAgentActivity` pings show as notices on that card; the
  card settles with the child's final message.
- Under `ask` mode, a child's command approval opens marim's panel with an
  agent-labeled request; under `plan` it is denied without a prompt.
- `GET …/history` and a resumed session show the spawn as a
  `spawn_agent` tool call with a result; no `codex_agent` cards anywhere.
- marim-mobile attached to a `marim serve` session sees the same card via
  `subagent.*` events without an app change.
- No behavior change for claude-cli, native spawns, aux clones, or a
  codex-cli turn that spawns nothing (fake-server suite untouched).

## Non-goals

- Driving Codex's collab tools from marim (`spawnAgent` as a marim command).
- Persisting child transcripts for the main-loop provider (follow-up).
- Changing the one-app-server-per-process model or the drop-unknown-thread
  rule — children are *registered*, never broadcast.
- Rendering Codex's `review`/`compact`/`memory_consolidation` sub-agent
  sources as cards; only `thread_spawn` children are user-visible work.
