# Context Management in Production AI Coding Agents (2026)

> Comparative analysis of compaction, sub-agent fan-out, memory, and tool-result
> truncation strategies used by Claude Code, Cursor, Aider, and OpenAI's Codex CLI /
> Agents SDK — with recommendations for a Pydantic-AI-based terminal harness.

---

## 1. Claude Code (Anthropic)

### Context Window
- **Standard:** 200K tokens; **Extended:** 1M tokens (with
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW` override) [docs.anthropic.com]
- Auto-compaction triggers at **~95%** of context capacity by default
  (`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`) [source-verified]

### Compaction Strategy
- **Full-session summarization:** When triggered, Claude Code sends the entire
  conversation to an LLM, generates a comprehensive summary, and **starts fresh
  with only that summary as initial context**. It does *not* preserve recent
  messages alongside the summary (that's Codex's approach) [verified via source
  code analysis].
- Manual `/compact` available for on-demand summarization.
- The summary includes: key decisions, file changes, current state, remaining
  tasks.

### Sub-agent Fan-out
- **Task tool** spawns sub-agents with their own context window. Each sub-agent
  gets a fresh context, does its work, and returns only a summary to the parent.
- Sub-agents can't spawn further sub-agents (no recursion).
- Useful for: codebase exploration, parallel file edits, running tests.

### Memory / Persistence
- **CLAUDE.md** (project root, subdirectories, user-level `~/.claude/CLAUDE.md`)
  — persistent instructions loaded every session.
- **Memory files** (`~/.claude/memory/`) — durable facts across sessions.
- **Session resumption** — full conversation history persisted and resumable.
- **Hooks** — SessionStart/UserPromptSubmit hooks can inject dynamic context.

### Tool Result Truncation
- File reads support offset/limit (range-based reading).
- Large tool results are truncated with a note; bash output has size limits.
- Prompt caching (`--cache-prompts`) reduces cost for repeated system prompts.

### Tradeoffs
- **Pro:** Simple, predictable compaction model. Full-session summary preserves
  narrative coherence.
- **Con:** Lossy — after compaction, all specific tool results, file contents, and
  intermediate reasoning are gone. Long sessions accumulate fidelity loss. No
  "keep recent N messages" grace period.

---

## 2. Cursor (Anysphere)

### Context Window
- **Auto mode:** ~20K tokens effective (conservative default for responsiveness)
- **Max Mode:** Full model context (200K for Claude Sonnet, up to 400K for GPT-5)
  [docs.cursor.com]
- Independent testing found usable context at 70K–120K after internal truncation
  [futureproofing.dev]

### Compaction Strategy
- **RL-trained self-summarization** (their "Composer" model is trained with
  compaction-in-the-loop) [cursor.com/blog/self-summarization]
- Triggers automatically at 100% context utilization
- The model pauses mid-generation at a fixed token trigger (40K/80K), summarizes
  its own context in scratch space, then continues
- **~1,000 tokens average summary** — roughly 1/5 the tokens of a
  prompt-based baseline (~5,000+ tokens)
- **~50% reduction in compaction error** vs. tuned prompt-based baseline
  [verified]
- Reuses KV cache across the summarization boundary (efficiency win)

### Sub-agent Fan-out
- **Three built-in subagents:** Explore (codebase search), Bash (shell commands),
  Browser (MCP-driven) — each in its own context window [docs.cursor.com/subagents]
- **Custom subagents:** Markdown files in `.cursor/agents/` with YAML frontmatter
  (name, model, readonly, is_background)
- **Cloud subagents** (`/in-cloud`): Run on isolated VMs with branch-based
  isolation
- **Tree coordination** (since v2.5): Subagents can launch child subagents (one
  level deep)
- **Background Agents:** Separate from subagents — run async on remote Ubuntu VMs,
  write to branches

### Memory / Persistence
- **Four rule types:** Project Rules (`.cursor/rules/*.mdc`), User Rules (global),
  Team Rules (dashboard), AGENTS.md
- **Four application modes:** Always Apply, Auto-attached (glob-based),
  Agent-decided (description-based), Manual (@-mention)
- **Dynamic context discovery:** Semantic code index (vectorized fragments in
  Turbopuffer) for just-in-time context fetching; ~46.9% token reduction vs.
  static loading [cursor.com/blog/dynamic-context-discovery]
- **Memory Bank:** Community-built workflow (not native) using structured
  `.cursor/docs/` files

### Tool Result Truncation
- Terminal output truncated at ~85KB [forum.cursor.com, staff-verified]
- Command results: ~20K character limit
- No per-tool range reading natively (unlike Claude Code's offset/limit)

### Tradeoffs
- **Pro:** Self-summarization is best-in-class for compaction quality. Dynamic
  context discovery avoids loading unnecessary data. Subagent tree coordination
  enables complex workflows.
- **Con:** Compaction is still lossy (degradation over multiple compressions). Auto
  mode's ~20K limit is very conservative. No user-facing setting to disable
  terminal truncation.

---

## 3. Aider

### Context Window
- **No self-imposed limit** — entirely model-dependent (16K for GPT-3.5-turbo,
  200K for Claude Sonnet 4, 1M for Claude Opus 4.7)
  [aider.chat/docs/troubleshooting/token-limits]
- Aider only reports provider token-limit errors; it never enforces its own cap
  [verified]

### Compaction Strategy
- **No built-in auto-compaction in the CLI** [verified]. This is the single
  biggest architectural difference from the other three.
- **Chat history summarization** via `ChatSummary` class: when chat history
  exceeds `max-chat-history-tokens` (default: 1024 output budget), it splits into
  a "tail" (recent messages, half budget) and "head" (older messages summarized
  by a "weak model" — default gpt-4o-mini). Recursive up to depth 3.
  [aider/history.py, source-verified]
- **AiderDesk GUI** (separate product) has `/compact` and auto-compaction at 100K
  tokens [verified]
- Manual commands: `/drop` (remove files), `/clear` (clear history), `/reset`
  (drop all + clear), `/tokens` (show usage)

### Sub-agent Fan-out
- **None in core Aider CLI** [verified]. Single-agent REPL architecture.
- **Architect mode** uses two models (architect proposes, editor applies) but
  they share the same conversation context — no isolation.
- AiderDesk has subagents (dedicated agent profiles), but this is a separate
  commercial product.

### Memory / Persistence
- **Repo map** (primary "memory"): Tree-sitter extracts class/function/method
  signatures, ranks via PageRank-like graph algorithm, binary-searches for max
  tags fitting in `max_map_tokens` (default 1,024). Expands up to ~8x when no
  files are in chat. [aider.chat/docs/repomap, verified]
- **CONVENTIONS.md:** Persistent coding conventions loaded via `.aider.conf.yml`
- **`.aiderignore`:** Gitignore-style patterns to exclude files (always enforced,
  can't be bypassed)
- **`.aider.conf.yml`:** Persistent config (model, map-tokens, read files, etc.)
- **Chat history:** Persisted to `.aider.chat.history.md` (markdown log);
  `--restore-chat-history` does lossy reconstruction
- **No cross-session memory** (each session starts fresh)

### Tool Result Truncation
- **None built-in.** Large file reads and shell outputs are passed verbatim.
  [verified from docs]
- Workarounds: pipe through `head`/`tail`, use MCP tools that self-truncate
- Prompt caching available (`--cache-prompts`) for cost, not context management

### Tradeoffs
- **Pro:** Maximum transparency and user control. Repo map is elegant and
  token-efficient. No hidden compaction surprises.
- **Con:** Requires active context stewardship in long sessions. No sub-agent
  isolation. No auto-compaction means context overflow is a constant risk with
  large repos. Not optimized for very large codebases.

---

## 4. OpenAI Codex CLI & Agents SDK

### Context Window
- **GPT-5.5 on Codex:** 400K tokens (capped from native 1M)
  [codex.danielvaughan.com, verified]
- **GPT-5.4:** 400K (raised from initial 256K)
- Extended context beyond 272K incurs 2x input pricing

### Compaction Strategy
- **Two-path architecture** [verified]:
  - **OpenAI models:** Server-side `POST /v1/responses/compact` returns
    AES-encrypted opaque blob. Client cannot inspect or modify.
  - **Other providers (Ollama, Azure, custom):** Local LLM summarization with
    compaction prompt.
- **After compaction:** Summary message + **up to 20,000 tokens of recent user
  messages** preserved [verified: `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000` in
  source]
- **Trigger:** ~95% of context window (not 90% as some sources claim) [verified
  correction]. Hard-capped; user config above 90% is silently ignored (since
  v0.100.0).
- **Dual trigger points:** Pre-turn (before sending new user message) and
  mid-turn (at loop boundary during long tool-call chains)
- **Manual `/compact`** with optional custom instructions (since v0.117.0:
  `/compact Focus on the authentication refactor`)
- Custom compaction prompts only affect the local path, not the server-side fast
  path

### Sub-agent Fan-out
- **Manual triggering only** — Codex spawns subagents when explicitly asked
  [developers.openai.com/codex/concepts/subagents]
- Each subagent runs in its own **agent thread** (inspectable via `/agent`
  command)
- Built-in types: `default`, `worker`, `explorer`; custom agents via TOML under
  `~/.codex/agents/`
- **Limits:** `agents.max_threads = 6` (concurrent), `agents.max_depth = 1`
  (spawn depth)
- Subagents inherit parent's sandbox policy; all share the same filesystem

### Memory / Persistence
- **Codex Memories** (opt-in): Carry context from prior threads via
  `~/.codex/memories/` files. Background generation, rate-limit-aware, skips
  active/short sessions, redacts secrets. [developers.openai.com/codex/memories]
- **Thread persistence:** Transcripts stored in `~/.codex/sessions/`; `codex
  resume` reopens with full history
- **AGENTS.md hierarchy:** Global -> project root -> subdirectories. Max 32 KiB
  default.
- **Chronicle** (research preview, macOS, ChatGPT Pro): Screen-capture-based
  memory extraction

### Tool Result Truncation
- **`tool_output_token_limit`** caps individual tool/function output
  contributions to context [verified in config reference]
- Recommended to lower to 8,000 for verbose tools (test suites, logs)

### Agents SDK Patterns
- **Session protocol** (SQLiteSession, RedisSession, etc.): Auto-maintains
  conversation history across `Runner.run()` calls
  [openai.github.io/openai-agents-python/sessions]
- **`OpenAIResponsesCompactionSession`:** Wraps any session with automatic
  server-side compaction via configurable `should_trigger_compaction` callback
- **`SessionSettings(limit=N)`:** Caps retrieved history to N most recent items
- **Handoffs with input filters:** Receiving agent can see full history or
  filtered subset. Nested handoffs (beta) collapse prior transcript into
  `<CONVERSATION HISTORY>` summary block.

### Tradeoffs
- **Pro:** 20K recent-token preservation is a smart hybrid (summary + recency).
  Server-side compaction is fast and KV-cache efficient. Agents SDK session
  abstraction is clean.
- **Con:** Encrypted summaries can't be audited (compliance concern). Compaction
  fidelity loss accumulates. "Compaction death spiral" was a real bug (fixed in
  v0.112 -> v0.117). Hard 90% cap overrides user config.

---

## 5. Comparison Table

| Dimension | Claude Code | Cursor | Aider | Codex CLI |
|---|---|---|---|---|
| **Context Window** | 200K standard / 1M extended | ~20K Auto / 200K-400K Max | Model-dependent (no self-limit) | 400K (GPT-5.5) |
| **Compaction Trigger** | ~95% auto | 100% auto | None (CLI) / 100K (AiderDesk) | ~95% auto + mid-turn |
| **Compaction Method** | Full-session LLM summary (replaces all) | RL-trained self-summarization (~1K tokens) | Weak-model chat history summary | Server-side encrypted (OpenAI) or local LLM |
| **Post-Compaction** | Summary only | Summary + continues | Summary of head + tail verbatim | Summary + 20K recent user tokens |
| **Sub-agents** | Task tool (no recursion) | Built-in + custom + cloud + tree (1 level) | None (CLI) / AiderDesk only | Manual, max 6 threads, depth 1 |
| **Memory** | CLAUDE.md + memory files + hooks | Rules (4 types) + semantic index + dynamic discovery | Repo map (PageRank) + CONVENTIONS.md + .aiderignore | Memories (opt-in) + AGENTS.md + Chronicle |
| **Tool Result Truncation** | Range reads + size limits | 85KB terminal / 20K command | None built-in | `tool_output_token_limit` config |
| **Session Resumption** | Full history | Full history | Lossy from markdown log | Full history via `codex resume` |
| **Key Strength** | Simplicity, narrative coherence | Compaction quality, dynamic discovery | Transparency, token efficiency | Hybrid summary+recency, speed |
| **Key Weakness** | Total fidelity loss on compaction | Conservative Auto limit, lossy multi-compact | No auto-compaction, manual stewardship | Opaque encrypted summaries, death-spiral risk |

---

## 6. Established vs. Hated

### Established (high-confidence patterns)

1. **Compaction is universal** — every production agent except Aider CLI does it
   automatically. It's not optional at scale.
2. **Sub-agent isolation is standard** — fan-out to fresh context windows is the
   primary mechanism for parallel/long-running work.
3. **Summary + recency hybrid beats pure summary** — Codex's approach (keep 20K
   recent tokens alongside the summary) is strictly better than Claude Code's
   "replace everything" for preserving immediate working context.
4. **RL-trained compaction is superior to prompt-based** — Cursor's 50% error
   reduction is the state of the art.
5. **Static context injection (rules/AGENTS.md) is table stakes** — all four
   support some form of persistent project instructions.

### Hyped / overrated

1. **"1M context solves the problem"** — even with 1M tokens, compaction is
   needed for multi-hour sessions. Context length delays but doesn't eliminate
   the problem.
2. **Encrypted server-side compaction** — fast but unauditable. A dealbreaker
   for compliance-sensitive environments.
3. **Semantic indexing as a context panacea** — Cursor's dynamic discovery helps
   but adds infrastructure complexity (vector DB) and latency.
4. **No-compaction transparency (Aider)** — principled but impractical for long
   sessions. Users hit token limits constantly.

---

## 7. Recommendations for a Pydantic-AI-Based Terminal Harness

Based on this analysis, here are concrete recommendations for marim-harness or
any Pydantic-AI terminal agent:

### 7.1. Adopt a "Summary + Recency" Compaction Model (Codex-style)

- **Don't** replace the entire conversation with a summary (Claude Code's approach
  loses too much).
- **Do** keep a rolling window of recent messages (last 15K-20K tokens) alongside
  a compacted summary of older history.
- **Implementation:** In your `SessionController`/compaction logic, split history
  at a token threshold. Summarize the older portion with a cheap model (like
  Aider's "weak model" pattern). Keep the recent portion verbatim.

### 7.2. Use a Separate "Weak Model" for Summarization

- Aider and Cursor both use cheaper models for compaction. This is
  cost-effective and fast.
- **Implementation:** Add a `compaction_model` config (default: a cheap, fast
  model). The main agent uses the expensive model only for actual coding
  decisions.

### 7.3. Implement Token-Budget-Aware Context Assembly

- Aider's repo map with binary search for token budget is elegant. Cursor's
  dynamic discovery is powerful but complex.
- **Implementation:** Before each turn, compute `token budget = context_window -
  system_prompt - expected_output`. Allocate remaining budget across: (a)
  persistent context (AGENTS.md/rules), (b) recent history, (c) older summary,
  (d) tool results. Use a priority queue — drop lowest-priority items first.

### 7.4. Sub-agent Fan-out with Context Isolation

- All three competitors (Claude Code, Cursor, Codex) support sub-agents. Aider's
  lack of them is a competitive weakness.
- **Implementation:** Your `spawn_agent` tool already provides this. Ensure
  sub-agents: (a) get a fresh context window, (b) return only a structured
  summary (not full history), (c) have configurable `max_depth` and
  `max_threads` to prevent unbounded fan-out.

### 7.5. Tool Result Truncation with Smart Defaults

- **Implementation:** Add per-tool output limits. For `bash`: truncate at ~50KB
  (keep tail, drop head). For `read_file`: support offset/limit natively (you
  already do). For `grep`: cap match count. Always tell the model *what* was
  truncated and offer a way to get more.

### 7.6. Persistent Memory with Explicit User Control

- **Implementation:** Your `remember`/`recall` system is good. Add: (a) automatic
  memory generation at session end (like Codex Memories), (b) project-level
  AGENTS.md loading (like all competitors), (c) a `memory_budget` to cap how
  much persistent context is injected per turn.

### 7.7. Configurable Compaction Threshold

- **Implementation:** Expose `compact_trigger_pct` (default 90%) and
  `compact_recent_tokens` (default 15000) as user config. Some users want
  earlier compaction for cost savings; others want maximum context.

### 7.8. Compaction Quality Monitoring

- **Implementation:** After compaction, log the compression ratio and summary
  size. Alert if summaries are too large (wasteful) or too small (lossy).
  Consider Cursor's RL-training approach long-term, but start with a
  well-crafted prompt.

### 7.9. Avoid the "Compaction Death Spiral"

- Codex had a bug where compaction fired too aggressively, causing cascading
  summarization.
- **Implementation:** Enforce a minimum time/tokens between compactions. Don't
  compact if the last compaction was <5K tokens ago. Use a hard floor (never
  compact below 50% of context).

### 7.10. Session Resumption with Clean History

- Your existing resumability invariant (never end with an unanswered
  ToolCallPart) is correct and matches best practices.
- **Implementation:** After compaction, persist the new summary + recent window
  as the resumable baseline. This ensures a resumed session starts from a clean,
  compacted state rather than re-compacting old history.

---

## 8. Priority Ranking for Implementation

| # | Borrow from | Change | Impact | Effort |
|---|---|---|---|---|
| 1 | **Aider** | Separate cheap model for summarization | ★★★★★ | Low |
| 2 | **Codex** | Configurable trigger % + token-based tail | ★★★★ | Low |
| 3 | **Codex** | Per-tool output token limits | ★★★★ | Low |
| 4 | **Cursor** | Richer summary prompt with structured metadata | ★★★ | Medium |
| 5 | **Aider** | Repo-map-style persistent codebase context | ★★★ | Medium |
| 6 | **Cursor** | Compaction quality logging/metrics | ★★ | Low |

---

## 9. Confidence Ratings

| Sub-question | Confidence | Main Limiting Factor |
|---|---|---|
| Claude Code context mgmt | **High** | Official docs + source code available |
| Cursor context mgmt | **High** | Blog posts with technical detail, but some claims from community forums |
| Aider context mgmt | **High** | Open source, code is the source of truth |
| Codex CLI context mgmt | **Medium** | Relies heavily on reverse-engineered blog posts; official docs are sparse on internals |
| Codex Agents SDK | **High** | Official Python SDK docs are comprehensive |
| Recommendations | **Medium** | Based on comparative analysis; actual effectiveness requires implementation testing |

---

## 10. Verification Notes

Key claims were adversarially verified by a separate sub-agent attempting to
refute them:

- **Claude Code compaction:** Confirmed at ~95% threshold. Correction: it does
  NOT keep recent messages — it replaces everything with a summary (unlike Codex).
- **Cursor self-summarization:** ~50% error reduction and ~1K token average
  summary size both confirmed from primary source.
- **Codex two-path compaction:** 20K recent tokens confirmed. Correction:
  threshold is 95%, not 90% as some sources claim.
- **Aider no auto-compaction:** Confirmed for CLI. Only AiderDesk GUI has it.
