# LESSONS - auto-maintained by scripts/lessons.py

> Machine-owned. Do NOT hand-edit. Changes are overwritten on the next `lessons.py` write.
> Canonical state lives in `.specs/lessons.json`. Edit lessons only via the script.
> promote_threshold=2 distinct features · window_days=45 · quarantine_threshold=2

## Confirmed (load these at Plan/Checks)

Corroborated across multiple features. Safe to apply as guidance.

_none_

## Candidates (under observation - do NOT load as guidance yet)

Seen once or not yet corroborated. Tracked, not trusted.

### L-001 - Invalidate stale context usage without discarding an independently valid window, and test recovery after invalidation.
- signal: `ac_gap` · recurrence: 1 feature(s) · scope: `cli-context` · harmful: 0
- features: cli-lifecycle-parity
- evidence: LIFE-11 (cli-context)
- last seen: 2026-09-14T20:05:49Z

### L-002 - Prove optional backend events from raw transport through the consuming view; directly injected display state cannot prove the adapter.
- signal: `ac_gap` · recurrence: 1 feature(s) · scope: `cli-events` · harmful: 0
- features: cli-lifecycle-parity
- evidence: LIFE-17 (cli-events)
- last seen: 2026-09-14T20:05:50Z

### L-003 - Test literal search with both a literal match and a nonmatching value that regex interpretation would match.
- signal: `ac_gap` · recurrence: 1 feature(s) · scope: `tests` · harmful: 0
- features: upstream-output-limits
- evidence: verification.md: C11 round-1 literal-search gap (tests)
- last seen: 2026-09-15T08:17:50Z

### L-004 - For delegated CLI providers, test observed usage before provider failure and cancellation; a missing ModelResponse must not erase attempt spend.
- signal: `ac_gap` · recurrence: 1 feature(s) · scope: `config/codex_cli_model.py` · harmful: 0
- features: codex-structured-output
- evidence: codex-structured-output/validation.md:STRUCT-07 (config/codex_cli_model.py)
- last seen: 2026-09-17T00:17:10Z

### L-005 - Check the ratchet force-selected complexity rules when adding parameters; default Ruff lint does not enable them.
- signal: `gate_fail` · recurrence: 1 feature(s) · scope: `quality-gate` · harmful: 0
- features: preserve-session-transcript
- evidence: verification.md: Resolved CI finding and fix evidence, PR151 run4572/job10390 (quality-gate)
- last seen: 2026-09-17T03:53:27Z

### L-006 - Assert tool-result content and receipt order in the TUI consumer; a tool-call widget and final answer do not prove result delivery.
- signal: `ac_gap` · recurrence: 1 feature(s) · scope: `tui-streaming` · harmful: 0
- features: codex-subscription-provider
- evidence: verification.md F1; tests/test_codex_subscription_surfaces.py:247 (tui-streaming)
- last seen: 2026-09-18T18:52:46Z

## Quarantined (failed when applied - ignore)

A confirmed lesson that recurred alongside failure. Kept for the maintainer to review.

_none_
