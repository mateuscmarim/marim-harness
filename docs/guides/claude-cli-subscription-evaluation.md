# Claude CLI subscription provider evaluation

Original assessment: 2026-09-19
Live run: 2026-09-19 — all ten cases executed against a real Claude
subscription and a real Claude Code binary.
Status: RUN. Cases 1–5 and 7–10 PASS. Case 6 is PARTIAL by explicit decision
(see its row); it is the one case that cannot be completed without rotating the
user's working grant.
Companion to the [Codex subscription provider evaluation](codex-subscription-evaluation.md),
which established the run discipline this page reuses.

The two providers are not symmetric, and the cases must not pretend they are.
Under `openai-codex` marim runs native Pydantic AI agents and owns the tools,
approval loop, sub-agents, sessions and compaction; that evaluation proved
*marim's* machinery on subscription traffic. Under `claude-cli` marim is a
launcher: one long-lived `claude` process per conversation runs its own tools,
LSP, MCP and compaction, and marim's contribution is the control-protocol
seam around it. What this page verifies is therefore **that seam**:

- permission gating through `can_use_tool` (`auto` / `ask` / `plan`),
- the approval panel and `ask_user` (headless denies both by design),
- interrupt, steer, and resume through the persisted `claude-cli:<id>`,
- `/mode`, `/model` and `/think` as control requests on the live process,
- Claude's own Agent sub-agents demuxed, rendered and **persisted** into
  marim's history, including a turn Claude runs on its own when a background
  agent reports,
- the reverse direction: a native main loop spawning a `backend: claude-cli`
  child, and native tiers routed through `claude-cli:<model>` targets,
- quota, context and cost reporting read from the process.

Offline scenario fakes (`tests/test_claude_cli_*.py`) establish the wire
shapes and deterministic behavior against a scripted process. The existing
live smoke (`MARIM_LIVE_CLAUDE=1 uv run pytest --no-cov -n 0
tests/test_claude_cli_live.py`) covers three of the seams below at the
`ClaudeCliModel` level, not through the production preset. Neither
establishes entitlement, coding quality, real quota consumption, or
credential behavior across processes. That is what this page is for.

## Run parameters

| Field | Value |
| --- | --- |
| Commit | `f7b2aaf5` |
| Date | 2026-09-19 |
| Provider | `claude-cli` (launcher: Claude Code runs its own tools; marim owns the control seam) |
| Claude Code binary | `2.1.278` (above `MIN_CLAUDE_VERSION` in `claude/env.py`) |
| Model | `haiku` (explicit `MARIM_MODEL`; global default untouched). Case 7 switches to `sonnet` mid-session on purpose |
| Thinking setting | unset for every case except case 7, which sets it deliberately |
| Permission mode | `auto`, except case 2 (`ask`) and case 3 (`plan`) |
| Temporary workspace | throwaway per-case fixture copies; isolated `XDG_DATA_HOME` per provider so no real marim session store was touched |
| Claude Code config | the child ran with `--setting-sources ""`, `--strict-mcp-config` and `--safe-mode` (`claude/process.py` `ISOLATION_ARGV`), so the user's own hooks, MCP servers and project settings did **not** load. Credentials came from `~/.claude/.credentials.json`, shared with the user's real Claude Code and not isolable without losing the login |
| Request limit | marim's per-turn request limit does not bound a Claude-side turn; each case was bounded with `MARIM_CLAUDE_CLI_TIMEOUT=300` |
| Entry point | `runtime/bootstrap.build_harness` with `MARIM_PROVIDER=claude-cli` — the production CLI preset, not a bespoke embedding path. Cases 9 and 9b additionally built a native harness; case 10b ran the real `marim serve` daemon |

Entitlement was confirmed **before** any generation, exactly as the codex run
did: `list_claude_models(strict=True)` performs a handshake-only launch (no
user message, no quota) and returns the account's `/model` menu plus
`account.subscriptionType`. The probe returned a populated menu, so the run
proceeded.

Consumption was observed on both sides. The process's `get_usage` quota
windows read **12% (5 h) / 46% (7 d)** before the first case and **14% (5 h) /
46% (7 d)** after the last — a +2 pp move on the five-hour window at
whole-percent resolution, which is the subscription evidence. marim's
`total_cost_usd` meter is a provider-reported figure, not a bill.

## Cases

Cases 1–6 mirror the codex page one-to-one so the two rows can be read side
by side. Cases 7–10 exist only for `claude-cli` because they exercise seams
the native provider does not have.

| # | Case | Expected observation | Outcome | Evidence |
| --- | --- | --- | --- | --- |
| 1 | File edit plus repository test | Claude edits the bounded fixture with its **own** Edit/Write tools and runs the named test successfully | PASS | Implemented `to_roman` with Claude's Edit tool; target bytes changed (`1a646e60…` → `7e2a443b…`); Claude's own run reported `2 passed in 0.00s` and an independent re-run by the evaluator exited 0 with the same summary. 1 request, 110,920 input / 1,222 output tokens, 13.9 s |
| 1b | Claude's tool calls persisted as real tool parts | Claude's tool activity lands in marim's history as tool parts, not only as `▸` text | PASS | With `bind_ui(on_cli_activity=…)` bound, the session file holds a `tool-call` / `tool-return` pair for `read_file` alongside the user prompt and the text. **Correction to the original proposal**: this assertion is UI-bound. `ActivityLedger.recording()` returns `None` when `on_cli_activity` is unbound, so a headless run has no ledger and folds the same calls into `▸` lines, which *are* its persisted record. Case 1 therefore shows zero tool parts, by design |
| 2 | Approval denial | In `ask` mode a Claude write reaches marim's approval seam and is denied; target bytes stay unchanged; Claude reports the denial instead of retrying around it | PASS | With a UI bound, the broker received one `can_use_tool` request classified as `edit_file` with the fixture path and the edit payload; `deny_reply` reached Claude, which answered "The edit was denied by the user." Without a UI, the headless path denied on its own and Claude described the change it would have made instead of hanging. Both runs left the file byte-identical (`1a646e60…` before and after) |
| 3 | Plan mode is local research | In `plan` mode a write is denied **and** `WebFetch`/`WebSearch` are denied; a read succeeds | PASS | `read_file` returned the fixture's contents; `write_file` and `web_search` were both denied with `plan mode: read-only — describe the change instead of making it`; `plan.txt` was never created. Two notes. The egress denial reuses the read-only wording rather than an egress-specific one — the accepted `PLAN_EGRESS_MESSAGE` follow-up from PR #113, named for a constant that does not exist yet. And **correction to the original proposal**: result events carry a generic `tool_name` of `"tool"`, so a denial must be attributed by pairing each result with its preceding call event |
| 4 | Interrupt, then resume | Interrupt an active turn; a fresh harness reopens the session by its `claude-cli:<id>` and completes a new turn that recalls the interrupted task | PASS | Turn confirmed still open at cancel; 1 message flushed; `cli_thread_id` persisted as `claude-cli:7a1bfff8-…`. A *fresh* harness relaunched with `--model haiku --resume 7a1bfff8-…` (asserted on `build_process_argv`) and recalled the seeded codename ORANGE-FALCON and the file name. Then, with a low `MARIM_CLAUDE_CLI_IDLE_TIMEOUT`, the process idle-closed (pid 3236632 → 3236831) and the next turn answered STILL-HERE on the **same** thread id |
| 5 | Context reduction followed by recall | Claude Code compacts its own context; marim receives the compaction notice, its context gauge drops, and the next turn still recalls the seeded facts | PASS | Two auto-compactions fired (`pre_tokens` 22,568 → `post_tokens` 1,640, then 21,920 → 2,448), each surfaced as `BackendNotice(kind="compaction")` with `trigger: auto`. The recall turn returned all three seeded facts verbatim (codename VIOLET-HARBOR, deploy window, owning team). Compaction is Claude's: it was provoked cheaply by lowering the child's own threshold (`CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=1` — a percent, `CLAUDE_` prefix) rather than by inflating the conversation |
| 6 | Process restart after credential refresh | A new process authenticates from the on-disk OAuth token after a refresh; if a re-login is needed, record the recovery | PARTIAL | Deliberately not completed. Filling it requires rotating the user's live grant, which the run rules forbid doing just to fill a row — the same decision the codex page records. What the other cases **did** establish: 16 distinct `claude` sessions were spawned across the run, plus two extra relaunches of one session id in case 4 and several handshake-only probe processes, and every one of them authenticated from `~/.claude/.credentials.json` with zero re-entry prompts. That file's mtime was unchanged across the whole run (`2026-09-19 09:38:53 -0300` before and after) |
| 7 | Live control parity | Mid-session `/mode`, `/model` and `/think` reach the running process as control requests without a rebuild | PASS | All four switches landed on the **same** pid 3239803 and the same thread `claude-cli:a350ebe6-…`. `/mode plan` → `set_permission_mode`, and the next write was denied on that live process. `/model sonnet` → `set_model`, pid and thread unchanged, the model replying SWITCHED. `/think high` → `set_max_thinking_tokens 32768` plus `apply_flag_settings {effortLevel: high}`. `/think off` on an adaptive model → budget 0 plus effort `low`, not an error. Eight control requests in all, each acknowledged |
| 8 | Claude's own sub-agents | A turn that makes Claude spawn an Agent yields a first-class sub-agent card and a persisted `spawn_agent` tool part; a **background** Agent's own turn is buffered and played as an autonomous turn | PASS | The demux emitted a synthesized `spawn_agent` call and its `ToolReturnPart` ("The sub-agent runs in the background; its report arrives as a task notification in a later turn."), both persisted to the session file. The child's model surfaced as `claude-haiku-4-5-20251001`. One autonomous turn was drained through `wait_backend_turn`, and the transcript ends with Claude's own summary: it found **4** `.py` files, which is correct |
| 9 | Native main loop → `claude-cli` child | A native harness spawns a `backend: claude-cli` child and the child's result is correct | PASS | Parent on `openai-codex:gpt-5.6-terra`; the `counter` child's sidecar records `backend: "claude-cli"`, `cli_session_id: 434b9d61-…` at depth 1, and the child returned `PYFILES=4` (correct). **Correction to the original proposal**: a `backend: claude-cli` spawn's sidecar carries `model_ref: null`, so the proof of provider is `backend` plus `cli_session_id` plus the child model reported through `on_subagent_model`, not `model_ref`. The free-model main loop the proposal assumed was unavailable — the stored zen key is stale (401) and LM Studio was offline — so the native side ran on the already-proven `openai-codex` subscription instead of a per-token paid model |
| 9b | Native tier routed onto the subscription | A native tier configured as `claude-cli:haiku` routes a plain spawn onto the subscription | PASS | With all three tiers set to `claude-cli:haiku`, a plain `explore` spawn launched `claude --model haiku` (asserted on `build_process_argv`) and its sidecar records `backend: "claude-cli"`, `model: "haiku"`, `cli_session_id: 87571ee7-…`. The spawn runs as a **background job**, so the one-shot turn returned before the child reported; after draining `JobRegistry` (4 s) the finished-job digest carried the child's answer and the follow-up turn reported 4 |
| 10 | Quota and context reporting | Quota windows and context usage read from the live process match what the TUI shows and persist across a resume | PASS | `read_usage` returned both windows with sane percentages (14% five-hour, 46% seven-day) and the hint rendered as `quota 14% (5h) · 46% (1w)`. After a filler turn `read_context_usage` grew from 21,211 to 26,328 against a 200,000 window. A simulated failed poll **cleared** the hint rather than leaving a stale reading, and the next good poll restored it |
| 10b | The same readings over `marim serve` | `GET session` exposes `context` and `quota` for the same session | PASS | A real daemon (`marim serve` v0.15.0, isolated `XDG_DATA_HOME`) ran one turn on `claude-cli:haiku`; `GET /v1/workspaces/{ws}/sessions/{sid}` returned `context: {used: 21220, window: 200000}`, `quota: "quota 14% (5h) · 46% (1w)"`, `compact_threshold: 100000`, and an exact cost |

Cases 2, 3, 7 and 8 ran once each on `haiku`. Case 1 is the only one that
measures coding ability, and `haiku` is the honest default for a subscription
evaluation: the pass means the cheapest tier cleared the fixture. It was not
repeated on `sonnet`, which stays available as a separate row.

## Observed consumption and attribution

44 ledger rows across the two `claude-cli` stores (38 on `haiku`, 6 on
`sonnet` from case 7's mid-session switch). **Every** row carries a
`backend_result` block — `num_turns`, `duration_api_ms`, `stop_reason`,
`permission_denials` — which only a CLI backend writes; not one of the 12
rows in the native `openai-codex` stores has it. That is the attribution
evidence: no claude-cli turn fell back to another provider, and no native
turn was served by the CLI.

One finding the run surfaced. The proposal expected each row's model column
to read `claude-cli:<model>`; it reads the **bare** id (`haiku`, `sonnet`)
instead. `usage_model_ref` qualifies only `openai-codex`, so a `claude-cli`
row is indistinguishable by model name alone from an API row on the same
model. Cost is unaffected — the CLI reports `cost_micro_usd` per turn, so
these rows are `cost_is_exact: true` rather than priced from a catalog — but
an operator reading the stats ledger has to use `backend_result` to tell the
two apart.

## What this evaluation cannot establish

- **Tool quality.** Claude Code's Edit/Read/Bash are Claude's; the fixture only
  proves marim relays their calls and results faithfully.
- **Compaction quality.** Case 5 proves marim observes and survives Claude's
  compaction; it says nothing about how good Claude's summary is.
- **Thinking.** `/think` maps to a thinking-token budget plus an effort level
  on the live process; whether the model actually reasons more is not
  observable from marim.
- **Monetary cost.** The provider's `total_cost_usd` is an estimate under a
  subscription. Unknown cost is not evidence of zero quota use.
- **Credential refresh.** Case 6 is partial: the run proves many processes
  share one on-disk token without re-login, not that a *refreshed* or expired
  token recovers cleanly.

## Run record required for each executed case

Record commit, date, Claude Code version, model id, thinking setting,
permission mode, temporary workspace, silence timeout, observable assertions,
token usage and quota delta, outcome, and an evidence path with credentials
and private content excluded. Use a fresh session per case; do not resume a
session from the user's real store. A failed or unavailable case stays
visible. Interrupt an agent that cannot finish the fixture.

Use the existing `claude` login on the machine running marim. Select
`MARIM_PROVIDER=claude-cli` and an explicit `MARIM_MODEL` only for the
evaluation process; never edit `.env`. Every case spends subscription quota
and needs an explicit OK from the user before it runs; the entitlement probe
and the quota read do not.

Per-case JSON records (assertions, usage, elapsed, result tail) were written
alongside the run, with no credentials and no private content. The fixture is
the same throwaway two-file Roman-numeral project the codex run used, so the
two case-1 rows are directly comparable.

## Status

Settled; nothing here is pending a decision. The provider ships, is opt-in
(`MARIM_PROVIDER=claude-cli`), and has offline scenario coverage plus a
three-test live smoke. What these rows add is evidence, not a verdict: the
launcher carries marim's gating, resumability, control parity, sub-agent
demux and reporting on real subscription traffic, and it does so while
Claude Code owns the tools. Re-run the page once per meaningful change to the
control seam. The genuine gaps left are case 6 (refresh and restart
reliability) and the ledger attribution finding above.
