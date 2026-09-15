---
name: deep-research
description: Produce a multi-source, fact-checked, cited research report. Use when the user wants deep research on a topic — fans out parallel researchers, adversarially verifies claims, then synthesizes.
---
# Deep research

Produce a thorough, cited research report by DELEGATING — do NOT do the research
yourself in this turn. Your job is to orchestrate researchers and synthesize their
findings.

## 1. Scope, then plan
Restate the question. Then do a quick SCOPING pass yourself — this is the one place you
research inline. If the domain is unfamiliar, run a couple of `web_search` calls to learn the
field's terminology, map the shape of the debate, and see what the real axes of disagreement
are. Skip the pass for topics you already know well; do NOT let it grow into full research.
(`web_search` is approval-gated — one more reason to keep the pass to a couple of queries.)

Scope FIRST because it makes any question to the user sharper — you only interrupt once, so
spend that interruption on what the landscape shows actually matters, not generic guesses.
After scoping, if scope/constraints are still ambiguous (region, timeframe, budget, use
case), ask the user 1–3 clarifying questions, then continue. The one exception: if the
question is so underspecified you cannot even search meaningfully, ask first.

Then decompose into 3–6 INDEPENDENT sub-questions that can be researched in parallel,
grounded in what the scoping pass surfaced — split along the seams you actually found (not
guessed), phrase each with the domain's real vocabulary, and check the set for gaps and
overlap so no two researchers cover the same ground.

## 2. Run the pipeline (one run_workflow call)
Start with ONE `run_workflow(code=...)` call implementing fan-out → coverage
check → adversarial verify, adapting the reference below. Replace `sub_questions`
with the literal list from your plan. The named `research_findings(task=...)` and
`verify_claim(task=...)` functions have fixed, validated report schemas: findings
contain claim/source/evidence_type/quality/load_bearing; verdicts contain
holds/downgrade/refuted and a reason. These functions must appear in the tool's
available catalog; otherwise use the fallback below.

The configured wall deadline (`MARIM_WORKFLOW_TIMEOUT`, default 1800 seconds)
includes child waits. There is no per-call timeout argument. The script returns
DATA (its last expression); you write the report from it afterward. `print` output
is captured in the final tool response; child cards provide live progress.

```python
# Deep research pipeline: fan out -> coverage -> adversarial verify
import asyncio

sub_questions = [
    "Evidence for cognition in healthy adults",
    "Evidence for cognition in special populations",
    "Dosing, safety, and study quality",
]

async def research(sub_q, sharpen):
    task = ("Research this sub-question and report findings, marking which are "
            "load-bearing: " + sub_q + sharpen)
    return await research_findings(task=task)

# Wave 1: one researcher per sub-question.
waves = await asyncio.gather(*[research(q, "") for q in sub_questions])
print("wave 1 done: " + str(sum(len(w["findings"]) for w in waves)) + " findings")

# Coverage: exactly ONE follow-up round for sub-questions that came back thin.
thin = [i for i in range(len(waves))
        if not any(f["load_bearing"] for f in waves[i]["findings"])]
if thin:
    print("coverage round for " + str(len(thin)) + " thin sub-questions")
    retries = await asyncio.gather(*[
        research(sub_questions[i], "\n\nA first pass found little; dig for primary sources.")
        for i in thin])
    for j in range(len(thin)):
        waves[thin[j]]["findings"] = waves[thin[j]]["findings"] + retries[j]["findings"]
        waves[thin[j]]["open_questions"] = (waves[thin[j]]["open_questions"]
                                            + retries[j]["open_questions"])

# Adversarial verify: try to refute each load-bearing claim.
flat = [f for w in waves for f in w["findings"]]
load_bearing = [f for f in flat if f["load_bearing"]]

async def refute(f):
    task = ("Try to REFUTE this claim, and confirm the cited source actually "
            "supports it. Claim: " + f["claim"] + " -- Source: " + f["source"])
    return await verify_claim(task=task)

print("verifying " + str(len(load_bearing)) + " load-bearing claims")
verdicts = await asyncio.gather(*[refute(f) for f in load_bearing])

dropped = []
for k in range(len(load_bearing)):
    f = load_bearing[k]
    v = verdicts[k]
    if v["verdict"] == "refuted":
        dropped.append({"claim": f["claim"], "reason": v["reason"]})
    else:
        f["verified"] = v["verdict"] + ": " + v["reason"]

kept = [f for f in flat
        if not f["load_bearing"] or "verified" in f]
{"findings": kept,
 "dropped": dropped,
 "open_questions": [q for w in waves for q in w["open_questions"]]}
```

The script returns data; the model writes prose. Do not synthesize inside the script.

**If a parallel child fails:** Monty 0.0.23 propagates worker failures out of
`asyncio.gather`, even if an async helper has a `try`/`except` around its call.
`gather(return_exceptions=True)` is not supported. The tool returns a correctable
error with bounded previews of completed results. Reuse complete, untruncated
results; finish only missing research/coverage/verification with a revised named
workflow call or the `spawn_agent` fallback. Do not blindly replay the full batch.
Treat truncated previews as incomplete evidence. Never label a claim verified
when its verification did not finish; mark it unverified or omit it.

## 3. Synthesize
From the returned bundle, write ONE report:
- Every nontrivial claim keeps its citation (the `source` field).
- Note verification: claims whose `verified` starts with "downgrade" are presented as
  weaker; `dropped` claims are omitted or explicitly called out as refuted.
- Where good sources genuinely DISAGREE, say so and explain why (effect size, trial
  quality, population) — do not flatten into a single verdict.
- End with: (a) 5 bullets separating what's well-supported from what's shaky or
  overstated, and (b) a per-sub-question confidence rating (high/medium/low) with
  the main limiting factor.

## If run_workflow is unavailable
Some installs lack the workflows extra or the two typed catalog bindings. Run the
same pipeline with `spawn_agent` directly: in a SINGLE turn, spawn one `researcher` per sub-question (`task` = the
sub-question; `returns` = "a list of findings; each = CLAIM + source + evidence type +
quality (high/medium/low) + whether it is load-bearing"). Collect the reports, then spawn
one `explore` refuter per load-bearing claim, tasked to refute it and confirm the cited
source supports it. Drop or downgrade what does not survive, then synthesize as above.
Weaker guarantees than the script (no schema validation, no coverage loop) — keep the
verify pass even so.

## Example
Topic: "Evidence on creatine for cognition (not muscle)." Sub-questions → researchers:
healthy adults; special populations (sleep-deprived, vegetarians, aging, mood);
dosing/kinetics for a brain effect; safety & study quality. Then refute the
load-bearing claims, then synthesize.
