# Upstream tool-output limits

Date: 2026-09-15
Status: Approved by the user on 2026-09-15; implemented, awaiting independent verification.
Profile: light (project default for tlc-spec-lean).
Branch: `refactor/upstream-output-limits`
Worktree: `/home/mateuscmarim/Projects/marim.dev/marim-harness/.worktrees/upstream-output-limits`
Feature base: `281630a7` (master at worktree creation).

## Problem

Marim maintains general output-size measurement, serialization, preview generation,
file spilling, and fallback clipping in several tool paths. That costs maintenance
even though Pydantic AI Harness now supplies those mechanics. The user wants to
prefer upstream behavior and remove redundant implementation, accepting reasonable
differences instead of wrapping upstream to reproduce every historical detail.

This migration transfers ordinary tool-return reduction to upstream. Marim retains
workspace permissions, media delivery, bounded producer buffers, session storage
selection, and explicit report budgets outside the ordinary tool-return lifecycle.
It is not a promise to remove a particular number of lines.

## Out of scope

- Compaction/masking algorithms, approval-loop replacement, StepPersistence,
  replacement subagent/workflow engines, or external CLI tool loops.
- New UI screens, HTTP endpoints, a new user-facing output-policy settings system,
  output summarization model calls, or adopting the upstream FileSystem/Shell stack.
- Removing file-read pagination, network download limits, command buffer bounds,
  image size limits, or command exit/timeout reporting.
- Rewriting existing saved sessions into a new format or deleting legacy files.
- Removing explicit `max_output_chars` report budgets or the workflow final-result
  cap in this pass. These also apply outside an agent tool hook; their limited
  remaining machinery is a documented exception, not another general limiter.
- Implementing #2 in this worktree. Its separate branch is
  `refactor/upstream-compaction`.

## Assumptions

| Assumption | Chosen default | Rationale | Confirmed? |
| --- | --- | --- | --- |
| Upstream defaults may replace historical details | Yes | Explicit user direction and the supplied upstream-first AGENTS.md policy | y |
| Ordinary text spill threshold | 10,000 characters, inclusive; upstream default preview and 4,000-character truncation fallback | Uses the selected upstream behavior instead of retaining the old 25,000-character formatting contract | n |
| Structured output layout | Upstream `indented_json` serializer | Structured results should remain usable through line-based retrieval | n |
| Spill retention | Session scratchpad lifetime; no independent TTL | Keeps temporary output with the existing session lifecycle | n |
| Scratchpad-disabled fallback | Workspace `.marim/output/upstream/<session-key>/`; a captured ephemeral key for a sessionless harness | Avoids upstream's process-global temp store and keeps fallback behavior within Marim's workspace | n |
| Explicit report budgets | Keep their existing application boundary and format in this migration | Upstream's default preview size is not a strict whole-return cap, and detached results bypass the tool hook | n |
| Mixed media | Preserve media-bearing results initially, including mixed content, with existing binary-safe rendering | An isolated probe showed direct large images become spill handles without a guard; generic recursive media rewriting would expand scope | n |
| Dependency release pair | Core 2.43.0 and Harness 0.31.0 for qualification | Matches the independently planned compaction release pair; lock exact versions during qualification | n |

Open questions: none. The user approved the proposed defaults above on 2026-09-15.
Full dependency and behavior compatibility remains an
implementation gate; the probes below do not establish it.

## Criteria

### S1 — One reduction mechanism for ordinary returns

**Acceptance criteria**

1. The native main agent and native subagents SHALL use the selected upstream
   `ToolOutputLimits` capability for eligible ordinary tool-return reduction.
2. WHEN eligible text is under 10,000 characters THEN the policy SHALL return it unchanged.
3. WHEN eligible text reaches 10,000 characters and storage succeeds THEN it SHALL
   become an upstream handle and preview whose stored
   payload equals the complete producer-returned text.
4. WHEN an eligible structured result crosses its band THEN its stored representation SHALL
   decode to the original JSON-compatible value.
5. IF a spill write raises an ordinary storage exception THEN the fallback SHALL
   contain at most 4,000 characters.
6. The default output policy SHALL make zero model requests for output reduction.

**Independent demonstration:** a deterministic native agent calls an ordinary
function tool and an MCP tool with small, boundary-sized, large, and structured
results; inspect the actual model request and retrieve the stored payload.

### S2 — Retrieval follows the owning session

**Acceptance criteria**

7. WHEN a session resumes with its spill files present THEN `read_tool_result` SHALL
   retrieve the payload referenced by its saved handle.
8. IF a saved upstream handle no longer has a file THEN retrieval SHALL return a
   model-readable missing-result response without rerunning the producing tool.
9. WHEN parallel calls have different tool-call identities THEN their handles SHALL
   retrieve their respective payloads without one overwriting the other.
10. WHEN a session switch occurs THEN a run or detached job already started SHALL
    retain its captured original spill destination.
11. IF a retrieval handle resolves outside its store root THEN retrieval SHALL
    disclose no file content from that target.
12. WHEN an old session contains an absolute output-file pointer THEN loading SHALL
    preserve the existing pointer-revalidation behavior.

**Independent demonstration:** spill in session A, persist/restore its transcript,
read through a fresh capability, switch to B, then finish A's pending work. Also
exercise a removed file and a symlink escape without executing the original tool.

### S3 — Existing execution contracts survive

**Acceptance criteria**

13. WHEN a tool returns supported media content THEN the output integration SHALL
    preserve the original media bytes for the model rather than replacing them
    with a text spill handle.
14. WHILE a command or network fetch is collecting output, the producer SHALL
    enforce its pre-migration collection bound before return reduction occurs.
15. The migration SHALL preserve tool approval decisions in auto, ask, and plan modes.
16. WHEN a native agent is configured with output reduction THEN it SHALL expose
    exactly one working `read_tool_result` tool, including under tool-search deferral.
17. WHEN a reduced result is saved THEN its history SHALL round-trip through
    `ModelMessagesTypeAdapter` with its tool-call/result pairing intact.
18. The migration SHALL retain the existing explicit subagent report-budget and
    workflow final-result contracts at their application boundaries.
19. The migration SHALL remove active ordinary-output calls to Marim's
    `offload_if_large`, fetch `_offload`, and MCP `_bound_tool_result` after their
    covered paths use upstream reduction.
20. WHEN command output contains an exit-status or timeout indicator THEN storage SHALL
    retain that indicator in the captured result for retrieval.

**Independent demonstration:** use native main/subagent runs with media and
deferred approvals, an oversized command, a budgeted detached report, and a
persisted transcript; run existing policy, producer-bound, and budget tests.

## Traceability

| ID | Slice | Criteria | Status |
| --- | --- | --- | --- |
| OUTPUT-01 | S1 | 1–6 | Implemented; proofs pass |
| OUTPUT-02 | S2 | 7–12 | Implemented; proofs pass |
| OUTPUT-03 | S3 | 13–20 | Implemented; proofs pass |

## Observable

| Surface | Decision | Landing |
| --- | --- | --- |
| Model tool returns | Small and empty text | AC 2 |
| Model tool returns | Large text and structured payloads | AC 3–4 |
| Model tool returns | Failed spill write | AC 5 |
| Model tool returns | Media-bearing payloads | AC 13; mixed media is a documented exemption from this text-size policy |
| Retrieval tool | Arguments, line ordering, bounded pages | Existing upstream public tool signature; Landing door 1 |
| Retrieval tool | Unknown/expired handle | AC 8 |
| Retrieval tool | Unauthorized filesystem target | AC 11 |
| Retrieval tool | Tool availability and naming collision | AC 16; reject duplicate user registrations at construction or first preparation |
| TUI and headless transcript | Preview and handle copy | AC 3 and AC 17; accept upstream wording |
| TUI command activity | Running output and partial failure | Existing bounded shell preview and command status handling; AC 14 and AC 20 |
| TUI approvals | Destructive actions | Existing approval panels and permission policy; AC 15 |
| CLI flags and exit codes | New flags / new exit status | n/a - no new command or CLI flag; existing turn error handling remains |
| Documentation | Explain changed threshold, handles, recovery and exceptions | Impact migration notes; readers use read_tool_result for new handles |
| Collection of spills | Naming, grouping and duplicates | AC 7, AC 9–10; upstream call identities under the owning session root |

## Flow

Reuse upstream serialization, size selection, spilling, truncation, and retrieval.
The integration wraps public hooks only to choose storage and preserve media.

1. Existing `HarnessBuilder` and bootstrap construct the native agent with a
   per-run output capability bound to the owning session's store root (AC 1, 10, 16).
   Native subagent construction applies the same policy using that spawn's captured
   session context; external CLI providers retain their own tool loop.
2. Existing function/MCP tools enforce their execution, permissions and producer
   bounds, then return their result without the old general offload envelope
   (AC 14–15, 19–20). Explicit report-budget boundaries remain in place (AC 18).
3. A thin media guard preserves supported media-bearing payloads. Eligible results
   pass to upstream's public `after_tool_execute`; it handles normal text and
   structured reduction, including `ToolReturn.return_value` and eligible textual
   `ToolReturn.content` (AC 2–6, 13). Do not inspect upstream private fields or
   reproduce its band-selection, preview, or serialization algorithms.
4. Upstream `LocalFileStore` writes under `<scratchpad>/tool-results`, or the
   session-keyed workspace fallback in Landing. The root is fixed for that run;
   session changes cannot redirect late writes (AC 7, 9–10).
5. Existing rendering and persistence receive the reduced result and upstream
   metadata. Preserve the wrapper instead of converting it to `str(ToolReturn)`
   (AC 17). PostToolUse and stream consumers see the final model-visible result.
6. Upstream `read_tool_result` uses the same root in later turns and resumed
   sessions; its own output is exempt from re-reduction (AC 7–8, 11, 16).
7. Existing session-load compatibility readers continue handling old absolute
   pointers. New upstream handles use upstream retrieval failure behavior rather
   than extending Marim's prose-matching regular expressions (AC 12).

For mixed media, retain the whole media-bearing value initially; do not JSON-stringify
image bytes to make the text-size check pass. Explicitly test both direct media and
media in `ToolReturn`/MCP content. Supporting arbitrarily large mixed-content text
without altering media is deferred unless the selected public API already supports
it; do not silently claim a universal payload limit.

## Relations

- One logical session owns one selected root for new upstream spills at a time.
- Many runs and native subagents in that session share that root; each run contains
  many tool calls, and one tool call may spill its return value and content separately.
- One persisted handle addresses one producer-returned payload within that root.
- A run/job retains its original root even when the interface switches sessions.
- Historical absolute pointers and new upstream handles can coexist in one history.
- No database tables or new session-state schema are introduced by this plan.

## Surface

None - no new HTTP routes, webhook responses, or CLI entry points.

The model-facing addition is the upstream function tool
`read_tool_result(handle, offset=0, limit=200, from_end=False, pattern=None)`.
Its observable outcomes are a bounded page, invalid-argument feedback, and a
missing/unreadable-handle response; these are tool results, not HTTP status codes.
Negative offsets are invalid; pattern matching is literal. New handle/preview
text follows upstream. Existing tool parameter names, CLI exit codes and server
protocol envelopes remain in place.

## Landing

| One-way door | Literal shape | Alternative rejected |
| --- | --- | --- |
| New histories reference upstream retrieval | `read_tool_result(handle, offset=0, limit=200, from_end=False, pattern=None)`; keep upstream handle strings and `overflow_handle` / `overflow_content_handle` metadata | Rewriting handles to the old absolute-path prose format would require owning another parser and retrieval protocol |
| Root of persisted handle resolution | `<scratchpad>/tool-results/`; fallback `<workspace>/.marim/output/upstream/<session-key>/` captured per run | Global `/tmp/pyai_harness_overflow` would share unrelated sessions; deleting at run end would break the next turn |

Keep readers available if this change is reverted after new histories are written.
There is no in-place rewrite of old session data. Changing scratchpad settings may
make old relative handles unavailable; document this and return the missing-result
message, never search other sessions or rerun side-effecting producers automatically.

## Impact

| Area | Change |
| --- | --- |
| Dependencies | Qualify core 2.43.0 / Harness 0.31.0, then update pyproject and lock together; install only required extras. Python >=3.10 remains required. |
| Ordinary result policy | Earlier spilling, upstream preview wording, upstream clipping fallback, and indented structured data; no paid summarization. |
| Tools and MCP | Remove general offload calls from fs/search/tree/glob, completed bash output, fetch, skill bodies/resources, and the MCP return hook. Preserve unrelated MCP configuration/trust logic. |
| Native subagents | Attach upstream reduction explicitly; parent embedder capabilities are not currently forwarded automatically. Keep explicit final-report budgets. |
| Low-level helper callers | Internal fs/fetch helpers may return more producer-bounded text; callers needing model-sized output must use the agent integration. Document any exported helper signature changes. |
| Compatibility | Keep constants, old handle readers and narrowly used report-cap helpers while callers still require them. Remove generic active writers only after all covered callers migrate. |
| UI and hooks | Adapt existing result unwrapping to upstream ToolReturn metadata and preview strings; preserve binary-safe rendering and CLI startup lazy imports. |
| Stored data | Old absolute pointers remain valid; new relative handles depend on the captured session root. No backfill. |
| Permissions | Retrieval is read-only within the selected root; reduction occurs only after authorized tool execution. Do not add a broad filesystem grant. |
| Bounded production | Keep shell buffers, download bounds, file pagination, cancellation/drain behavior and partial-output notices. Spilling cannot recover data discarded by these bounds. |
| Other migration | Compaction owns old-history clearing/summarization and legacy readers. This feature owns ordinary new-output reduction and retrieval. Do not change compaction policy here. |
| Maintenance measure | Report removed active writers and net source changes after implementation. The earlier 200–350-line estimate is provisional; media and report-budget integration may reduce the saving. |

## Release qualification and evidence

An isolated Python 3.13 probe ran with installed metadata confirming
`pydantic-ai-slim==2.43.0` and `pydantic-ai-harness==0.31.0`. It used public
`RunContext`, `ToolOutputLimits.after_tool_execute`, `LocalFileStore`, and
`FunctionToolset.get_tools/call_tool`; no private API calls.

- 9,999-character text passed unchanged; 10,000-character text spilled.
- A fresh LocalFileStore at the same root recovered the complete text.
- Structured data serialized with indented_json decoded to the original object;
  the store contained multiple lines and retrieval selected the final requested row.
- An unknown handle returned a missing-result message.
- A store that raised OSError produced a 4,000-character text fallback.
- A direct 20,000-byte BinaryContent image became a ToolReturn spill, not the original
  image. This proves the need for AC 13's media-preservation integration.

This is API feasibility evidence, not a passing Marim integration test. It does not
establish full dependency resolution, actual provider rendering, cancellation,
parallel-call identities, hostile filesystem paths, or Python 3.10/3.14 behavior.

## Delivery and verification approach

Use independently verifiable slices; this is the intended path, not a frozen task list.

1. Qualify the exact dependencies and the public integration boundary. Verify the
   version pair in an isolated worktree environment and exercise actual Agent runs,
   media returns, storage failure and retrieval before removing production code.
2. Route ordinary main-agent/MCP results through upstream and remove superseded
   callers. Verify S1 plus permissions, rendering and bounded producer behavior.
3. Apply the policy to native subagents and session transitions. Verify S2/S3,
   explicit foreground/detached report budgets, tools under deferral, and old/new
   session fixtures. Document the remaining specific exceptions.
4. Verify the combination with the compaction branch in a disposable integration
   worktree: large output -> clear/summarize -> save -> resume -> retrieve, including
   missing spill files. If #2 has not landed, run against the existing compactor and
   retain the cross-branch scenario as an explicit integration obligation.

Before build, derive proof-backed checks from this reviewed plan, including all nine
dimensions: validation/bounds (S1, AC 14), partial failure (AC 5, 8), retry/duplicates
(AC 9 plus provider tool-ID reuse), authorization (AC 11, 15), concurrency/order
(AC 9–10), data lifecycle (AC 7–8, 12), dependency failure (AC 5 and release
qualification), state transitions (AC 7, 10, 17), and observability (the final
result observed by the model, UI and hooks). No new telemetry service is required.

Run checks in repository order: `uv run ruff check src tests`, `uv run pyright`,
then `uv run pytest`; complete `uv build` and the supported Python 3.10/3.12/3.14
matrix before release. Preserve meaningful existing invariant tests; revise tests
that exclusively require an intentionally retired preview format, identifying that
behavior change explicitly. Do not relax unrelated assertions to pass an upgrade.

A fresh independent verifier reviews the implementation against the approved checks
after build, using the light profile and naming its limits. No live provider spend,
push, merge, or release is part of this planning request.

## Parallel-work boundary

- Coordinate dependency updates: both branches use the same selected versions; merge
  or cherry-pick one qualified dependency commit and regenerate the final lock once.
- Likely overlap: pyproject/lock, native subagent capability construction, and legacy
  offload imports in compaction.py. Resolve by responsibility, not by taking either
  file wholesale.
- Compaction's plan currently retains tool-level offloading. This migration replaces
  its ordinary producer implementation; it does not remove compatibility readers or
  assume old elision pointers use the new store.
- Leave the existing compaction worktree and its uncommitted changes untouched.
- The caller-supplied upstream-first AGENTS.md applies to this worktree even though
  the master commit used as its base predates that uncommitted instruction edit.

## Sources

- User discussion: prefer upstream, accept reasonable defaults, plan #1 in a worktree.
- Supplied AGENTS.md upstream-first convention and existing permission/resume invariants.
- Source audit at feature base: tools/impl/offload.py, fs.py, shell.py, fetch.py;
  mcp/config.py; tools/skill_tools.py; workspace/agents.py; subagents/runner.py;
  workflows/engine.py and schema.py; workspace/scratchpad.py; runtime/builder.py.
- [Upstream output-limits documentation](https://pydantic.dev/docs/ai/harness/tool-output-limits/).
- [Pinned public implementation](https://github.com/pydantic/pydantic-ai-harness/blob/v0.31.0/pydantic_ai_harness/tool_output_limits/_capability.py).
- [Pinned store implementation](https://github.com/pydantic/pydantic-ai-harness/blob/v0.31.0/pydantic_ai_harness/tool_output_limits/_store.py).
- [Pinned package metadata](https://github.com/pydantic/pydantic-ai-harness/blob/v0.31.0/pyproject.toml).
- Independent compaction proposal inspected in the original checkout at
  docs/superpowers/specs/2026-09-15-upstream-compaction-design.md; that proposal is
  coordination context, not implementation owned by this plan.
