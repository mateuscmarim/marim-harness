---
name: deep-research
description: Produce a multi-source, fact-checked, cited research report. Use when the user wants deep research on a topic — fans out parallel researchers, adversarially verifies claims, then synthesizes.
---
# Deep research

Produce a thorough, cited research report by DELEGATING — do NOT do the research
yourself in this turn. Your job is to orchestrate sub-agents and synthesize their
reports.

## 1. Plan
Restate the question, then decompose it into 3–6 INDEPENDENT sub-questions that can be
researched in parallel. If the question is too vague to research well (missing scope,
constraints, region, or timeframe), ask the user 1–3 clarifying questions FIRST, then
continue.

## 2. Fan out (parallel)
In a SINGLE turn, call `spawn_agent` once per sub-question:
- `type`: `researcher`
- `task`: the sub-question, stated precisely
- `context`: the overall research question and why this sub-question matters
- `returns`: "A list of findings; each = CLAIM + source URL + type
  (meta-analysis/RCT/observational/other) + quality (high/medium/low)."

Spawn them together so they run concurrently. Do NOT research inline.

## 3. Verify (adversarial)
Collect the workers' findings. For each load-bearing claim — the ones your conclusion
depends on — call `spawn_agent` with `type`: `explore` and a task to REFUTE it: find
counter-evidence and confirm the cited source actually supports the claim. Drop or
downgrade any claim that does not survive.

## 4. Synthesize
Write ONE report:
- Every nontrivial claim keeps its citation.
- Where good sources genuinely DISAGREE, say so and explain why (effect size, trial
  quality, population) — do not flatten into a single verdict.
- End with: (a) 5 bullets "established vs. hyped", and (b) a per-sub-question
  confidence rating (high/medium/low) with the main limiting factor.

## Example
Topic: "Evidence on creatine for cognition (not muscle)." Sub-questions → researchers:
healthy adults; special populations (sleep-deprived, vegetarians, aging, mood);
dosing/kinetics for a brain effect; safety & study quality. Then refute the
load-bearing claims, then synthesize.
