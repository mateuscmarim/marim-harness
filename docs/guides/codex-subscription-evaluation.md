# Codex subscription provider evaluation

Original assessment: 2026-09-18
Live run: 2026-09-19 — cases 1–5 executed against a real Codex subscription.
Status: PARTIALLY RUN. Cases 1–5 PASS. Case 6 remains NOT RUN by explicit
decision (see its row); it is the one case that cannot be filled without
rotating the user's working grant.
Historical decision: keep this provider opt-in and retain `codex-cli`.
Superseded on 2026-09-18 by the user-authorized CLI executor removal. The
original NOT RUN rows below have been replaced by the 2026-09-19 results.

Offline integration proofs establish wiring and deterministic behavior. They do
not establish model entitlement, coding quality, real subscription consumption,
or credential rotation across real processes. The rows below marked PASS are
live: each ran a fresh native session against the account's own `codex login`
credentials and consumed real subscription quota.

## Run parameters

| Field | Value |
| --- | --- |
| Commit | `b3c43470` |
| Date | 2026-09-19 |
| Provider | `openai-codex` (native Pydantic AI agents, marim owns tools/permissions/sub-agents/sessions) |
| Model | `gpt-5.6-terra` (explicit `MARIM_MODEL`; global default untouched) |
| Reasoning setting | provider default (`MARIM_THINKING` unset) |
| Permission mode | `auto`, except case 2 (`ask`) |
| Temporary workspace | throwaway per-case fixture copies; isolated `XDG_DATA_HOME` so no real session store was touched |
| Request limit | 40 model requests per turn; 20 per sub-agent |
| Entry point | `runtime/bootstrap.build_harness` — the production CLI preset, not a bespoke embedding path |

Entitlement was confirmed independently of generation: the catalog endpoint
returned five visible models for the account (`gpt-6-astra`, `gpt-5.6-sol`,
`gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`) through upstream-owned OAuth.

## Cases

| Case | Expected observation | Outcome | Evidence / reason |
| --- | --- | --- | --- |
| File edit plus repository test | Agent edits a bounded fixture project and runs its named test successfully | PASS | Implemented `to_roman` in the fixture; target bytes changed; `python -m pytest -q test_romans.py` → `2 passed`, re-run independently by the evaluator after the turn. 4 requests, 37,247 tokens, 19.5 s |
| Approval denial | Deny a requested write in ask mode; target bytes stay unchanged | PASS | `edit_file` deferred to the approval seam and was denied; target bytes byte-identical afterwards; the model reported the edit was rejected rather than retrying around it. 4 requests, 36,688 tokens |
| Native child execution | One granted native child runs on the selected subscription provider and reports its result | PASS | The `counter` child ran and returned `PYFILES=2` (correct). Its sidecar records `model_ref: openai-codex:gpt-5.6-terra` at depth 1, and it nested a depth-2 `explore` child (tier `cheap`) on the same provider — so child execution and bounded nesting both ran on the subscription. 17,757 tokens for the parent turn |
| Interrupt/resume | Interrupt one active turn; resume the saved session and complete a new turn | PASS | Turn cancelled mid-flight (confirmed still running at cancel); 7 messages flushed to the store; a *fresh* harness reopened the same session id, replayed all 7, and completed a new turn that correctly recalled the interrupted task. The provider accepted the repaired history, so `_repair_unanswered_tool_calls` holds on this backend |
| Context reduction followed by recall | Reduce a bounded fixture conversation and continue using its retained task facts | PASS | Forced reduction ran the `micro+summary` stage: 14 messages → 6. The following turn recalled all three seeded facts (codename, deploy window, owning team) in **1 request with no tool call**, so recall came from the retained summary, not a file re-read |
| Process restart after credential refresh | Record whether a new process can authenticate after refresh; if login is needed, record the recovery and effect on CLI access | NOT RUN | Deliberately skipped. Filling this row requires rotating the user's live grant, which the run instructions forbid doing just to fill it; the user was asked and chose to skip. What *is* established: five separate OS processes each authenticated from the existing on-disk token with no re-login |

A caveat on case 5: the threshold and `keep_messages` knobs were tightened in
process (to 4) before forcing the pass. The CLI preset keeps the last 20
messages verbatim, so a genuinely bounded fixture is exempt from summarization
and the first two attempts were honest no-ops. Tightening the documented knob
was preferred over inflating the conversation to burn quota. The summarizer
itself ran on the subscription.

## Observed consumption

25 ledger rows across all attempts (including two discarded case-5 no-op runs
and one case-2 re-run), totalling 350,151 input and 4,460 output tokens.
**Every** row is attributed to `openai-codex:gpt-5.6-terra` — no row fell back
to another provider, which is itself the evidence that the turns were served by
the subscription rather than a configured default. Monetary cost is not
observable from the client; that is not evidence of zero quota use.

## Run record required for each executed case

Record commit, date, provider/model id, reasoning setting, permission mode,
temporary workspace, request limit, observable assertions, token usage, outcome,
and an evidence path with credentials and private content excluded. Use a fresh
native session; do not convert an existing CLI session. A failed or unavailable
case stays visible. Unknown monetary cost is not evidence of zero quota use.

Use the existing login flow on the machine running Marim. Select
`MARIM_PROVIDER=openai-codex` and an explicit `MARIM_MODEL` only for the evaluation
process. Do not alter the global default. Keep each trial bounded with the
existing request limits and interrupt an agent that cannot finish the fixture.
Run an equivalent CLI fixture only when the user also requests that comparison.

Per-case JSON records (assertions, usage, elapsed, result tail) were written
alongside the run. They contain no credentials and no private content. The
fixtures were throwaway copies of a two-file Roman-numeral project; the
session store was redirected to a temporary `XDG_DATA_HOME`.

## Status of the replacement

Settled; nothing here is pending a decision. The `codex-cli` executor was
removed on 2026-09-18 and native `openai-codex` is its shipped replacement — a
persisted `codex-cli` selection now fails with migration guidance
(`config/retired.py`) rather than falling back silently, and old CLI
transcripts stay readable. The provider is opt-in by design
(`MARIM_PROVIDER=openai-codex`), which is the intended end state, not an
interim one.

What these rows add is evidence, not a verdict: cases 1-5 confirm that the
native provider carries marim's own tools, approval gating, sub-agent tiering
and nesting, session resumability, and context reduction on real subscription
traffic. The one genuine gap left is case 6 — refresh/restart reliability —
which is unfilled test coverage, not a blocked decision. It stays open until
someone runs it deliberately, accepting a possible re-login.
