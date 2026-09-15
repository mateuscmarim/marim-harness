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

## Quarantined (failed when applied - ignore)

A confirmed lesson that recurred alongside failure. Kept for the maintainer to review.

_none_
