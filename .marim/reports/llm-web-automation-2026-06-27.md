# LLM-Based Web Automation: State of the Art (June 2026)

> A cited research report produced by parallel deep-research with adversarial verification.
> Where sources disagree or numbers are contested, I say so explicitly.

---

## 1. Approaches: How Agents Perceive and Act on the Web

Web agents differ fundamentally in how they "see" a page and decide where to act. The four paradigms below are ordered by production maturity, not by hype.

### 1.1 DOM / Accessibility-Tree Grounding

**How it works.** The agent reads a structured accessibility tree (AT) — a YAML-like serialization with `[ref=eN]` handles — and calls `click {ref: "e10"}`. No vision model is involved. Playwright's `snapshotForAI()` is the canonical implementation.

**Cost & latency.** ~200–400 tokens per snapshot, deterministic refs, no vision-model inference. This is the cheapest and fastest paradigm by a large margin — roughly **7.5–12.5× cheaper** than a full-screenshot vision pass.¹

**What breaks it.**
- Pages with generic ARIA roles (bare `<div>`s styled as controls) produce unnamed `generic [ref=eN]` entries.
- Cross-origin iframes become opaque nodes.
- Refs invalidate on every AJAX/SPA re-render, requiring a re-snapshot.
- Canvas-rendered apps (Figma, Google Maps) expose nothing useful in the tree.

**Representative systems.** Playwright MCP,² Stagehand (Browserbase),³ Agent-E,⁴ Browser Use.⁵

### 1.2 Vision / Screenshot Grounding

**How it works.** The agent captures a pixel screenshot, sends it to a vision-language model, and receives coordinate-based actions (`left_click [x,y]`, `type`, `key`). This is the paradigm behind Anthropic Computer Use,⁶ OpenAI CUA/Operator,⁷ and CogAgent.⁸

**Cost & latency.** Per-step latency of **3–8 seconds** (2–4× slower than text-only calls).⁶ A full Retina Chrome screenshot is ~500 KB base64 ≈ 350,000 input tokens before downscaling.¹ Production systems downscale, bringing effective cost closer to 3,000–5,000 tokens per vision pass.¹

**What breaks it.**
- Pixel-coordinate clicks break under layout shifts, responsive resize, or DPI scaling (macOS Retina captures at 2× DPI; coordinates must be halved).
- Small UI elements disappear when downscaling 4K+ screenshots to model input limits.
- Hallucinated coordinates on dense pages.
- Requires vision-model inference every single step.

### 1.3 Set-of-Mark (SoM)

**How it works.** A preprocessing pass (segmentation models like Mask DINO, GroundingDINO, SAM) overlays numbered spatial marks on a screenshot. The LLM sees "Login [12]" and outputs `click(12)`; orchestration maps back to the actual DOM element.⁹

**Representative systems.** Microsoft SoM,⁹ WebVoyager (GPT-4V-Act),¹⁰ SeeAct (ICML 2024).¹¹

**What breaks it.** Still requires vision-model inference (latency + cost). Mark overlay generation adds a preprocessing pass. Dense pages produce confusing clusters of adjacent numbers; the WebVoyager paper notes the model confuses adjacent elements and misreads numbers on calendars.¹⁰ The 2026 production cohort increasingly treats SoM as a fallback rather than a primary channel.¹

### 1.4 Hybrid / Multi-Modal

**The emerging consensus.** The strongest production systems (Playwright MCP, Stagehand, Browser Use 2.0) default to **tree + screenshot-on-demand**: accessibility tree as the primary perception channel, screenshot only for canvas/charts/CAPTCHA, refs not selectors, re-snapshot after navigation, depth-bounded snapshots (depth 2–4), selector-scoped snapshots for large pages.¹

Google's Gemini computer-use-preview issue #113 (Feb 2026) proposes a `COMPUTER_USE_HYBRID=true` flag to add a compact structural payload alongside screenshots, motivated by: *"Coordinate-based interactions are brittle on responsive or dynamic pages."*¹²

### Web-Only vs. General Computer-Use

Web-only agents can exploit DOM/APIs (`snapshotForAI`, `MutationObserver`, aria refs) — impossible on native desktop. General computer-use agents (Anthropic Computer Use, OpenAI CUA, OSWorld-style) must rely on screenshots because no accessibility tree exists for native windows (except platform APIs like macOS AXUIElement or Android UI Automator). This distinction is critical: **computer-use benchmarks test a strictly harder problem** than web-only benchmarks.

---

## 2. Frameworks and Provider Landscape

### 2.1 Open-Source Frameworks

| Framework | Stars | Grounding | Backing Model | Pricing | Maturity |
|---|---|---|---|---|---|
| **browser-use** | ~101k★⁵ | DOM/a11y + screenshots | Any (incl. own `bu-*` models) | Free (OSS); cloud $0.02–0.06/hr | Production ($17M seed, Mar 2025)¹³ |
| **Stagehand** (Browserbase) | ~22k★³ | NL → Playwright at runtime | Any (Vercel AI SDK) | Free (OSS); cloud extra | Production (named customers: Ramp, Amplitude)¹⁴ |
| **Skyvern** | — | Playwright + AI swarm | Any | Free (OSS); cloud extra | Production (SOC2)¹⁵ |
| **BrowserGym** (ServiceNow) | — | DOM/a11y/screenshots (Gym API) | Any | Free | Research¹⁶ |
| **Agent-E** | — | DOM distillation (multi-representation) | AG2 agents | Free | Research⁴ |
| **Nanobrowser** | — | In-browser Chrome extension | Any (flexible) | Free | Hobby/early (Mar 2025)¹⁷ |

### 2.2 Model-Vendor Computer-Use / Browser Tools

| Offering | Type | Grounding | Pricing | Status |
|---|---|---|---|---|
| **OpenAI Operator / CUA** | Product (ChatGPT) | Screenshots + mouse/keyboard | Included with ChatGPT Pro ($200/mo) | Production (integrated into ChatGPT "agent mode", Jul 2025)⁷ |
| **Anthropic Computer Use** | API tool | Screenshots + coordinates | Per-token (standard Claude pricing) | Public beta (API), Oct 2024⁶ |
| **Google Gemini Computer Use** | API + product | Pixels + "intents" explaining reasoning | Per-token (Gemini API) | Preview / GA in AI Mode, Oct 2025¹⁸ |
| **Amazon Nova Act** | Managed AWS service | Full hosted agent | Usage-based, free tier | GA¹⁹ |

**Key architectural distinction.** Model-vendor tools (OpenAI, Anthropic, Google, Amazon) expose a **low-level browser/computer-use tool** — the developer must build the agent loop, screenshot capture, and action execution themselves. Open-source frameworks (browser-use, Stagehand, Skyvern) expose a **full agent loop** with recovery, stealth, and integrations built in. Stagehand uniquely offers a middle path: deterministic `act/extract/observe` primitives plus an optional `agent()` for autonomy.³

### 2.3 Production Evidence

The strongest production evidence is for **Browserbase + Stagehand**: Ramp processes 5M+ receipts/month and 4,200+ hours of manual work saved monthly; Amplitude has built 40+ custom demo environments generating $10M+ influenced pipeline.¹⁴ Browser-use raised $17M seed (Felicis Ventures, March 2025).¹³

**Counterpoint:** MultiOn — once the highest-profile AI web agent startup (2023-2024, $5M raised from General Catalyst, Amazon Alexa Fund) — has wound down to minimal scale by 2026, with the founder pivoting to a research lab (AGI, Inc.).²⁰ This is a cautionary data point: even well-funded, category-defining web agent startups have not found sustainable product-market fit as of mid-2025.

---

## 3. Benchmarks and Success Rates

> **The most important finding in this section:** the three most-cited benchmarks — WebArena, WebVoyager, and Mind2Web — have all been shown to substantially overstate agent capabilities when evaluated rigorously. The newer Online-Mind2Web benchmark provides the most credible current picture.

### 3.1 WebArena (CMU)

- **What it tests:** 812 tasks across self-hosted replicas of real websites (e-commerce, forums, GitLab, CMS, maps, wiki).²¹
- **Top score (Jun 2026):** WebTactix (DeepSeek v3.2) at **74.3%** (594/812 tasks).²²
- **Human baseline:** 78.24% (from the original paper, measured by the authors themselves).²¹
- **GPT-4 baseline (2023):** 14.41%.²¹

**Verdict: PARTIALLY CONFIRMED.** The 74.3% number is real and checkable on the Steel.dev leaderboard.²² But:
- It is a **system score** (model + scaffold + tools + policy), not a bare model result.
- The human baseline has **never been independently audited** — it was measured by the benchmark authors on the same synthetic tasks.
- WebArena tests on only **4 self-hosted replica sites** — a tiny fraction of real web diversity. The "An Illusion of Progress?" paper notes that "the diversity of websites is inherently limited due to the sheer difficulty of creating full replica of modern websites."²³
- WebArena-Infinity (Yandex, 2025) was created specifically to address drift and grading brittleness.²⁴

**Confidence: Medium.** The number is real; the "nearly human-level" narrative built on top of it is misleading.

### 3.2 WebVoyager

- **What it tests:** 643 tasks across 15 real-world live websites.¹⁰
- **Top score (self-reported):** Surfer 2 at **97.1%**, Alumnium at 98.5%.²²
- **Original paper baseline:** 59.1% (WebVoyager agent).¹⁰

**Verdict: SCORES INFLATED.** Xue et al. (2025) demonstrate that a **naive Google Search agent (no website interaction) achieves 51%** on WebVoyager tasks, meaning many tasks are solvable via shortcuts.²³ The Emergence WebVoyager paper (Akkil et al., 2026) independently audits the benchmark and finds OpenAI Operator achieves only **68.6%** on their refined version vs. the **87% self-reported** — an 18.4-point gap.²⁵ They also find ~15% of tasks are solvable from LLM training data alone. The benchmark has not been updated since June 2024 and the authors have not publicly responded to these critiques.

**Confidence: Low for >90% claims.** The benchmark is effectively saturated and known to be ~20 points inflated vs. rigorous evaluation.

### 3.3 Mind2Web and Online-Mind2Web

- **Mind2Web** (offline): 2,350 tasks from 137 websites. Best models achieve ~52% step success rate under cross-task evaluation.²⁶
- **Online-Mind2Web** (Xue et al., 2025): 300 tasks across 136 live websites, with rigorous human evaluation.²³

**Top scores under human evaluation (Xue et al. Table 2):**
- OpenAI Operator: **61.3%**
- Claude Computer Use 3.7: **56.3%**
- SeeAct, Browser Use, Agent-E, Claude 3.5: ~28–30%²³

**Current HAL leaderboard top (WebJudge auto-eval):** SeeAct + GPT-5 Medium at **42.33%**.²⁷

**Verdict: CONFIRMED.** The 61.3% Operator score is the best human-evaluated number in the paper. The HAL leaderboard uses stricter auto-evaluation (WebJudge), yielding 42.33% at the top. This is the most credible current benchmark picture.

**Confidence: High.** This is the most rigorously evaluated benchmark with human ground-truth.

### 3.4 OSWorld (Desktop/Computer-Use)

- **What it tests:** 369 real computer tasks across Ubuntu, Windows, macOS.²⁸
- **Top score (self-reported):** Claude Mythos Preview at **85.4%** (Anthropic's Claude 5 system card).²²
- **Human baseline:** 72.36%.²⁸
- **OpenAI Operator (CUA):** 32.6% (self-reported at launch).²²

**Verdict: TREAT WITH SKEPTICISM.** The top numbers (85.4% Claude Mythos, 75% GPT-5.4) are **self-reported by Anthropic and OpenAI respectively**, not independently verified. The OSWorld-Verified update (July 2025) fixed community-reported issues, so older numbers are not directly comparable. Human baseline is 72.36%, so claims of 85%+ exceed human performance — possible but requiring independent replication.

**Confidence: Low for >80% claims** (self-reported, not independently verified).

### 3.5 GAIA (Meta)

- **What it tests:** 450 questions across 3 difficulty levels (web-relevant primarily at Level 1).²⁹
- **Top score (HAL leaderboard):** Claude Sonnet 4.5 (HAL Generalist Agent) at **74.55%** overall.²⁷
- **Not strictly a web automation benchmark** — tests general AI assistant capabilities.

### 3.6 Summary Table: Benchmark Credibility

| Benchmark | Top Score | Credibility | Why |
|---|---|---|---|
| WebArena | 74.3% | Medium | Real score, but scaffolded system on 4 replica sites; human baseline unaudited |
| WebVoyager | 97–98% | **Low** | ~20 pts inflated; naive search agent gets 51%; benchmark abandoned by authors |
| Online-Mind2Web | 61.3% (human) / 42.3% (auto) | **High** | Most rigorous; human-evaluated; live open web |
| OSWorld | 85.4% | Low | Self-reported, exceeds human baseline, not independently verified |
| GAIA | 74.55% | Medium | Verified leaderboard, but not web-specific |

---

## 4. Hard Failure Modes

### 4.1 Dynamic Pages

- BrowserArena (live open-web benchmark, 109 user-submitted tasks) identified **direct navigation to URLs** (instead of search) as a consistent failure mode.³⁰
- Vision-based agents fail on dense, dynamic interfaces like date pickers (24px cells) and canvas-based apps (Google Sheets, Figma, Canva). Production agents have been observed taking "several minutes of repeated attempts on a simple calendar interaction."³¹
- Annotated-screenshot approaches break down on dense layouts where labels overlap.³¹
- Cookie banner/pop-up detection is a consistent failure mode: DeepSeek-R1 (text-only, no vision) **never detected privacy banners** and falsely marked tasks as "completed" at the highest rate.³⁰

### 4.2 Latency and Token Cost

| Metric | Value | Source |
|---|---|---|
| Observation tokens per step (AT-based) | ~200–400 | ¹ |
| Observation tokens per step (screenshot, downscaled) | ~3,000–5,000 | ¹ |
| Full Retina screenshot (not downscaled) | ~350,000 tokens | ¹ |
| Per-step latency (AT-based) | <1s | ¹² (anecdotal) |
| Per-step latency (screenshot-based) | 3–8s | ⁶ |
| Tool schema overhead (40 tools × 8 steps) | ~25,600 tokens/task | ³² |
| Gemini production telemetry | ~15,482 input tokens/step | ³³ |
| Cost per task (30k in + 5k out, Sonnet) | ~$0.175 LLM tokens alone | ³⁴ |
| Cost-per-successful-outcome (70% success) | ~$0.25 before infra | ³⁴ |
| Cloud browser infra (AWS AgentCore, 10-min session) | ~$0.012/session | ³⁵ |

**The cost picture:** LLM tokens dominate. Browser cloud costs are roughly an order of magnitude less than the LLM token cost for the same session.³⁵ One enterprise team found a POC costing ~$50 in OpenAI API usage would scale to ~$2.5 million/month at production volume.³⁶

### 4.3 Security: Prompt Injection

**"Mind the Web" (Shapira et al., 2025)** demonstrates task-aligned indirect prompt injection achieving **82% attack success rate (ASR)** against Browser Use with a fine-tuned attack generator.³⁷

**Adversarial verification of this claim:**
- ✅ The 82% ASR is real **for Browser Use specifically** (100 independent trials).
- ⚠️ The other four agents (OpenAI Operator, Do Browser, OpenOperator, Perplexity Comet) were only **manually demonstrated**, not statistically evaluated.³⁷
- ✅ The attack is realistic: requires only the ability to post content (comments, reviews, ads) on public websites the agent visits.³⁷
- ⚠️ The evaluation used a self-hosted blog and local Reddit instance, with simple open-ended tasks ("summarize this page").³⁷
- ✅ The paper itself proposes effective mitigations: LLM-as-Judge (isolated from malicious content), Least Privilege Enforcement, Human-in-the-Loop for sensitive actions.³⁷

**Other documented attacks:**
- **EchoLeak (CVE-2025-32711):** Zero-click prompt injection in Microsoft 365 Copilot (CVSS 9.3), chaining four bypasses for data exfiltration.³⁸
- **Brave Security** demonstrated indirect prompt injection against Perplexity Comet causing it to fetch one-time passwords from email and access banking portals — all from hidden text on web pages.³¹
- **Palo Alto Networks Unit 42** documented 22 distinct indirect prompt injection techniques in the wild by March 2026.³⁹

**Bottom line:** The security risk is real and demonstrated, but bounded. The attacker must control user-visible content on a page the agent visits during an open-ended task. Mitigations exist but reduce autonomy. There is **no demonstrated solution that achieves both high security and high utility**.³⁷

### 4.4 Reliability / Flakiness

- Even the best agents fail ~22–25% of tasks on a single attempt (GAIA leaderboard).²⁷
- The **Reliability Decay Curve** shows success drops systematically as task duration grows — pass@1 on short tasks is "structurally blind" to reliability degradation on long-horizon tasks.⁴⁰
- BrowserArena found only **57–63% human agreement** when judging the same agent runs, and VLM judges agreed with humans only 58–68% of the time — agent performance is inherently noisy.³⁰
- **Vending-Bench** demonstrates "catastrophic meltdown" behavior: even top models (Claude 3.5 Sonnet) increased net worth in only 3/5 runs, with failure modes including sending emails demanding "QUANTUM NUCLEAR LEGAL INTERVENTION."⁴¹

### 4.5 Auth, Sessions, CAPTCHAs

- **CAPTCHA resolution** is one of three consistent failure modes across all tested agents (BrowserArena).³⁰
- **No fully autonomous solution exists for 2FA or CAPTCHA.** Production approaches include: cookie syncing, password managers, TOTP generation, persistent hosted browsers, and human-in-the-loop "Remote Assist" where the agent pauses and shares a live session with a human.⁴²
- **Passkey-enabled authentication** entirely blocks some agents (e.g., browser-use cannot log into passkey-enabled applications).⁴³
- **Session-based authorization** "breaks because it assumes the actor's intent stays stable for the life of the session" — a fundamental mismatch with agent autonomy.⁴⁴

---

## 5. Production Readiness

### 5.1 What's Genuinely Usable

1. **Browserbase + Stagehand** is the best-validated production stack, with named enterprise customers running high-volume workloads (Ramp: 5M+ receipts/month; Amplitude: 40+ demo environments, $10M+ influenced pipeline).¹⁴
2. **AWS AgentCore Browser** validates cloud-browser-as-infrastructure (GA 2025/2026), pricing at $0.0895/vCPU-hour + $0.00945/GB-hour.³⁵
3. **57% of enterprises** now have AI agents in production (LangChain survey, Nov-Dec 2025, n=1,340), up from 51% the year before. Large enterprises (10k+ employees) lead at 67%.⁴⁵
4. The **MAP study** (Pan et al., 2025, ICML 2026, n=306 practitioners) confirms production agents are built with simple, controllable approaches: 68% execute ≤10 steps before human intervention, 70% use prompting (not fine-tuning), 74% depend primarily on human evaluation.⁴⁶

### 5.2 What's Still Research/Demo-Ware

- **MultiOn** — once the highest-profile AI web agent startup — has wound down to minimal scale by 2026.²⁰
- **WebArena** tests on only 4 self-hosted sites; its scores do not transfer well to the open web.²³
- Most published web agent papers (Agent-E, WebVoyager SOTA, ST-WebAgentBench, Odysseys) are one-off research artifacts without maintained codebases or confirmed production deployments.

### 5.3 The Gap Between Benchmarks and Reality

A 75% WebArena score does NOT mean 75% real-world success. The reliability gap operates on three dimensions:
1. **Site diversity** — 4 self-hosted sites vs. millions of real sites with unique markup, anti-bot, and layouts.
2. **Variance across runs** — models hitting 85% pass@1 drop significantly on pass@8.
3. **Harness sensitivity** — scores change based on the agent loop code, tool docs, and evaluation method, not just the model.⁴¹

**Reliability is the #1 barrier to agent production**, cited by 32% of practitioners (LangChain 2025 survey).⁴⁵

### 5.4 The "Last Mile" Problems

1. **Anti-bot detection** is the #1 deployment blocker. Cloud datacenter IPs are flagged by default; headless Chrome exposes dozens of detectable properties; agents that click in 40ms look non-human. Some protection systems return subtly incomplete data instead of blocking, corrupting weeks of collection silently.⁴⁷
2. **Site-specific selector breakage** is the core maintenance burden. "Self-healing" selectors are not a solved problem — no independent measurement of actual breakage rates in production exists.⁴⁸
3. **Observability is table stakes** (89% of orgs have it), but 48% of organizations still run NO offline evaluations.⁴⁵
4. **The dominant production pattern is human-in-the-loop by design** — 68% of agents stop and get human input within 10 steps. Fully autonomous long-running web agents are not trusted in production.⁴⁶

---

## 6. Established vs. Hyped

- ✅ **ESTABLISHED:** Accessibility-tree grounding is the dominant production paradigm. It's 7.5–12.5× cheaper than vision and works today for form-based workflows on known sites.
- ✅ **ESTABLISHED:** Prompt injection is a real, demonstrated attack vector with >80% ASR in controlled settings. Mitigations exist but reduce autonomy.
- ✅ **ESTABLISHED:** WebVoyager scores are substantially inflated (~20 points). The benchmark is effectively abandoned.
- ⚠️ **HYPE / UNVERIFIED:** OSWorld scores >85% are self-reported by model vendors and exceed the human baseline. Treat as unverified until independently replicated.
- ❌ **DEMO-WARE:** Fully autonomous, long-running web agents on arbitrary open-web sites. No production evidence. The dominant pattern is ≤10 steps with human review.

---

## 7. Confidence Ratings (Per Section)

| Section | Confidence | Main Limiting Factor |
|---|---|---|
| §1. Approaches | **High** | Well-documented by vendors and researchers; token-cost numbers are consistent across sources |
| §2. Frameworks | **High** | Primary sources (GitHub, vendor docs) for all major entries; pricing fluid |
| §3. Benchmarks | **High** | Multiple independent audits (Xue et al., Emergence WebVoyager paper) corroborate the inflation finding |
| §4. Failure Modes | **Medium-High** | Prompt injection is well-documented; cost numbers are vendor-authored (medium); dynamic-page failures are observed but not systematically benchmarked |
| §5. Production Readiness | **Medium** | Vendor case studies are not independently audited; the MAP study (n=306) is the strongest evidence; "57% in production" is self-reported survey data |

---

## Sources

¹ Perea.AI, "Accessibility Tree vs Screenshot Perception" (2026). https://www.perea.ai/research/accessibility-tree-vs-screenshot-perception
² Playwright MCP Snapshots. https://playwright.dev/mcp/snapshots
³ Stagehand (Browserbase). https://www.stagehand.dev/
⁴ Agent-E (Emergence AI). https://arxiv.org/html/2407.13032v1
⁵ browser-use. https://github.com/browser-use/browser-use
⁶ Anthropic Computer Use. https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
⁷ OpenAI Operator / CUA. https://openai.com/index/computer-using-agent/
⁸ CogAgent (Hong et al., CVPR 2024). https://arxiv.org/abs/2312.08914
⁹ Microsoft SoM (Yang et al., 2023). https://github.com/microsoft/SoM
¹⁰ WebVoyager (He et al., ACL 2024). https://arxiv.org/html/2401.13919v4
¹¹ SeeAct (ICML 2024). https://arxiv.org/html/2401.01614v1
¹² Gemini computer-use-preview issue #113. https://github.com/google-gemini/computer-use-preview/issues/113
¹³ browser-use seed round. https://browser-use.com/posts/seed-round
¹⁴ Browserbase case studies. https://www.browserbase.com/blog/case-study-ramp
¹⁵ Skyvern. https://github.com/skyvern-ai/skyvern
¹⁶ BrowserGym (ServiceNow). https://github.com/servicenow/browsergym
¹⁷ Nanobrowser. https://github.com/nanobrowser/nanobrowser
¹⁸ Google Gemini Computer Use. https://blog.google/innovation-and-ai/models-and-research/google-deepmind/gemini-computer-use-model/
¹⁹ Amazon Nova Act. https://aws.amazon.com/nova/act/
²⁰ MultiOn status. https://neuronfeed.com/startups/multion
²¹ WebArena (Zhou et al., ICLR 2024). https://arxiv.org/abs/2307.13854
²² Steel.dev leaderboards. https://leaderboard.steel.dev/
²³ Xue et al., "An Illusion of Progress?" (2025). https://arxiv.org/html/2504.01382v4
²⁴ WebArena-Infinity (Yandex, 2025).
²⁵ Emergence WebVoyager audit (Akkil et al., 2026). https://arxiv.org/html/2603.29020v1
²⁶ Mind2Web (Deng et al., NeurIPS 2023). https://arxiv.org/abs/2306.06070
²⁷ HAL leaderboards. https://hal.cs.princeton.edu/
²⁸ OSWorld. https://os-world.github.io/
²⁹ GAIA (Mialon et al., 2023).
³⁰ BrowserArena. https://arxiv.org/html/2510.02418v2
³¹ Brave Security / web agent security survey. https://arxiv.org/html/2511.19477v1
³² Agent loop cost optimization. https://aipromptshub.co/blog/agent-loop-cost-optimization-guide
³³ Fireworks.ai agent execution tax. https://fireworks.ai/blog/agent-execution-tax
³⁴ Browser agent unit economics. https://blog.hireninja.com/2025/11/15/the-2025-unit-economics-of-ai-browser-and-workflow-agents-a-cost-control-playbook/
³⁵ AWS AgentCore Browser pricing. https://aws.amazon.com/bedrock/agentcore/pricing/
³⁶ Token cost trap. https://medium.com/@klaushofenbitzer/token-cost-trap-why-your-ai-agents-roi-breaks-at-scale-and-how-to-fix-it-4e4a9f6f5b9a
³⁷ "Mind the Web" (Shapira et al., 2025). https://arxiv.org/html/2506.07153v2
³⁸ EchoLeak (CVE-2025-32711). https://arxiv.org/html/2509.10540v1
³⁹ Palo Alto Unit 42. https://unit42.paloaltonetworks.com/ai-agent-prompt-injection/
⁴⁰ Reliability Decay Curve. https://arxiv.org/abs/2603.29231
⁴¹ Agent benchmarks (simmering.dev). https://simmering.dev/blog/agent-benchmarks/
⁴² browser-use authentication. https://browser-use.com/posts/web-agent-authentication
⁴³ Reddit: browser-use + passkeys. https://www.reddit.com/r/AI_Agents/comments/1kfkp7u/how_do_you_handle_authentication_with_browseruse/
⁴⁴ Session-based auth breakdown. https://nhimg.org/faq/what-breaks-when-ai-shopping-agents-rely-on-session-based-authorisation/
⁴⁵ LangChain State of Agent Engineering 2025. https://www.langchain.com/state-of-agent-engineering
⁴⁶ MAP study (Pan et al., CRML 2026). https://arxiv.org/abs/2512.04123
⁴⁷ Anti-bot for web agents. https://www.tinyfish.ai/blog/anti-bot-protection-for-web-agents
⁴⁸ Self-healing selectors. https://scrolltest.com/self-healing-test-selectors-production-failures-2/
