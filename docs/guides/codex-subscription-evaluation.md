# Codex subscription provider evaluation

Date: 2026-09-18
Status: NOT RUN — live subscription evaluation has not been requested.
Historical decision: keep this provider opt-in and retain `codex-cli`.
Superseded on 2026-09-18 by the user-authorized CLI executor removal. The
following original evaluation rows remain historical, not new completion claims.

Offline integration proofs establish wiring and deterministic behavior. They do
not establish model entitlement, coding quality, real subscription consumption,
or credential rotation across real processes. No row below is a live success.

## Cases

| Case | Expected observation | Outcome | Evidence / reason |
| --- | --- | --- | --- |
| File edit plus repository test | Agent edits a bounded fixture project and runs its named test successfully | NOT RUN | No live subscription request has been authorized or issued |
| Approval denial | Deny a requested write in ask mode; target bytes stay unchanged | NOT RUN | No live subscription request has been authorized or issued |
| Native child execution | One granted native child runs on the selected subscription provider and reports its result | NOT RUN | No live subscription request has been authorized or issued |
| Interrupt/resume | Interrupt one active turn; resume the saved session and complete a new turn | NOT RUN | No live subscription request has been authorized or issued |
| Context reduction followed by recall | Reduce a bounded fixture conversation and continue using its retained task facts | NOT RUN | No live subscription request has been authorized or issued |
| Process restart after credential refresh | Record whether a new process can authenticate after refresh; if login is needed, record the recovery and effect on CLI access | NOT RUN | No live credential refresh has been authorized or issued; do not rotate the user's grant just to fill this row |

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

## Replacement decision

No default switch or CLI removal is approved. After live evidence exists,
summarize failures and missing behavior, including refresh/restart reliability,
and ask for a separate decision. A passing mocked suite does not close this step.
