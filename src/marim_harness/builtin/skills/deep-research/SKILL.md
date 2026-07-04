---
name: deep-research
description: Produce a multi-source, fact-checked, cited research report. Use when the user wants deep research on a topic — fans out parallel researchers, adversarially verifies claims, then synthesizes.
---
# Deep research

Produce a thorough, cited research report by DELEGATING — do NOT do the research
yourself in this turn. Your job is to orchestrate sub-agents and synthesize their
reports.

## 1. Scope, then plan
Restate the question. Then do a quick SCOPING pass yourself — this is the one place you
research inline. If the domain is unfamiliar, run a couple of `WebSearch` calls to learn the
field's terminology, map the shape of the debate, and see what the real axes of disagreement
are. Skip the pass for topics you already know well; do NOT let it grow into full research.

Scope FIRST because it makes any question to the user sharper — you only interrupt once, so
spend that interruption on what the landscape shows actually matters, not generic guesses.
After scoping, if scope/constraints are still ambiguous (region, timeframe, budget, use
case), ask the user 1–3 clarifying questions, then continue. The one exception: if the
question is so underspecified you cannot even search meaningfully, ask first.

Then decompose into 3–6 INDEPENDENT sub-questions that can be researched in parallel,
grounded in what the scoping pass surfaced — split along the seams you actually found (not
guessed), phrase each with the domain's real vocabulary, and check the set for gaps and
overlap so no two researchers cover the same ground.

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
