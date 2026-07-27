# Interactive Project Trust — Design

**Date:** 2026-07-26
**Status:** Approved for planning

## Problem

Everything a cloned repo could use to run code on startup or inject prompt
content — `.marim/hooks.json`, `.marim/mcp.json`, project plugins, plugin LSP
manifests, `.marim/skills/`, `.marim/agents/` — is gated on
`MARIM_TRUST_PROJECT_HOOKS` (`src/marim_harness/trust.py`). The gate is
correct but its only interface is an env var nobody discovers: untrusted
projects **silently drop** their skills/hooks/MCP, and users burn a debugging
session finding out why (observed live: project skills missing in a serve
session, root-caused to the unset env var).

Goal: keep the fail-closed security property, add an *informed, persistent,
per-project* trust decision with first-class surfaces in the TUI, the CLI,
and the serve API.

## Decisions (settled with the user)

1. **Remembered both ways** — a decline persists too; untrusted projects show
   a one-line notice instead of re-prompting. Re-prompt only when the
   project's executable surface changes.
2. **One binary decision** — trust everything project-local or nothing. The
   dialog lists what will load so the choice is informed. No tiers.
3. **Hot-apply on grant** — granting trust reloads the gated surface live
   (no restart). Revoke persists but warns that already-running
   hooks/MCP/LSP processes stop only on restart.
4. **Headless honors the store** — `marim -p` / `marim serve` consult the
   same stored decision (no prompting anywhere non-interactive). Env var
   stays the override in both directions.
5. **CLI + serve get their own mechanism** — a `marim trust` subcommand and
   `GET/POST /v1/trust` endpoints, all writing the same store and driving the
   same reload seam.

## Architecture

One store, one live state, one reload seam, three front-ends:

```
                 ┌─ TUI TrustPanel (first open)
store  ◄─────────┼─ marim trust grant/revoke      (persist decision)
(trusted-        └─ POST /v1/trust
 projects.json)
      │ read at bootstrap + on demand
      ▼
trust resolution: explicit config → env var → store → untrusted
      │
      ▼
TrustState (live, session-scoped)──► lazy readers (skills/agents discovery,
      │                              instructions, plugin discovery)
      ▼
Harness.apply_project_trust() ─────► eager reloads (hooks config swap,
                                     MCP project-server connect,
                                     LSP registry rebuild)
```

### 1. Trust store (`trust.py` grows; stays a leaf module)

File: `$XDG_STATE_HOME/marim-harness/trusted-projects.json` (fallback
`~/.local/state/marim-harness/`). State, not data: machine-local operator
decisions, never inside the repo.

```json
{
  "/abs/resolved/workspace/root": {
    "trusted": true,
    "fingerprint": "<canonical surface JSON>",
    "decided_at": "2026-07-26T21:00:00Z"
  }
}
```

- Keyed by **resolved** workspace root path.
- `"trusted": false` is a remembered decline.
- A stored decision is honored **only while the stored fingerprint matches
  the project's current executable surface**. Mismatch ⇒ entry treated as
  absent (re-prompt in TUI, untrusted headless).
- Atomic write + advisory file lock, same pattern as the plugin registry
  (`plugins/install.py` / `atomic_io.file_lock`).
- Corrupt/unreadable store ⇒ treated as empty (fail closed), warning logged.

**Project surface fingerprint.** Canonical JSON over the *executable*
project surface: `.marim/hooks.json` resolved entries, `.marim/mcp.json`
server specs, and each project-scope plugin's executable surface (reusing
the shape of `executable_surface_fingerprint` in `plugins/install.py`).
Inert content (skills/agents text) deliberately does **not** feed the
fingerprint — same policy as plugins: editing a skill must not drop trust.
An unreadable/invalid config file fingerprints as an empty section so any
later real content registers as a change.

**Resolution order.** Explicit caller decision →
`MARIM_TRUST_PROJECT_HOOKS` env → store lookup (fingerprint-fresh) →
untrusted. The store lookup needs the workspace root, which the leaf
predicate `project_trusted()` doesn't take — so full store-aware resolution
happens where the root is known: a new `resolve_project_trust(workspace_root,
explicit)` in `trust.py`, called once by bootstrap/builder to seed the
session's `TrustState` (and re-called by the trust front-ends on change).
`project_trusted()` keeps its current explicit→env→untrusted behavior for
the unthreaded call sites, which migrate to reading `TrustState` where they
have access to it. The env var now overrides in
*both* directions: truthy ⇒ trusted, **explicit falsy ⇒ force-untrusted
regardless of store** (today falsy and unset are equivalent; unset keeps
falling through).

**Tri-state config.** `HarnessConfig.trust_project_hooks` (
`config/model.py:178`) changes `bool = False` → `bool | None = None`, read
via a tri-state env helper (`None` when the var is unset). Every
`trust_project=cfg.trust_project_hooks` call site keeps working because
`project_trusted(explicit=None)` falls through. Callers that today pass a
computed `False` meaning "unset" must be audited to pass `None`.

### 2. Gated-surface summary (new pure helper)

`ProjectSurface` value object built by scanning the workspace: counts and
names for hooks (with event names), MCP servers, skills, agents, plugins;
plus the fingerprint. Pure/side-effect-free; used by the TUI panel, `marim
trust`, `GET /v1/trust`, and bootstrap's "should we prompt?" check.
`surface.empty` ⇒ never prompt, never notice.

### 3. First-open flow (TUI)

`bootstrap.build_harness`: when the surface is non-empty and no decision
exists (env unset + store miss/stale), build **untrusted** and attach a
`TrustPrompt` payload (surface summary + fingerprint) to the harness.
`HarnessApp.on_mount` mounts `TrustPanel(InteractionPanel)` — inline above
the status bar like approval/plan panels, transcript stays scrollable:

```
This project ships configuration that loads on startup:
  hooks: 2 (SessionStart, PreToolUse) · mcp: 1 (docs-server) · skills: 3 · agents: 1
Trust this project?  [T]rust   [D]on't trust        docs/guides/trust.md
```

- **Trust** → persist `{trusted: true, fingerprint}` → `apply_project_trust()`
  → panel closes, one-line confirmation in transcript.
- **Don't trust** → persist `{trusted: false, fingerprint}` → notice:
  `Project config present but not trusted — /trust to enable.`
- Panel never blocks typing a prompt; an unanswered panel = untrusted for
  this session (fail closed), and the panel stays until answered or session
  ends.

### 4. Live trust state + reload seam

**`TrustState`** — a small session-scoped object holding the current
decision, reachable from `Deps`/services (same lazy-read pattern as
`thinking_level_id`). Lazy readers (skills/agents discovery, instructions,
plugin discovery) consult it per use, so they flip on the next turn with no
reload. `discover_skills`' cache already keys on the trust flag, so no cache
poisoning across the flip.

**`Harness.apply_project_trust()`** — the grant path's eager reloads:

- **hooks**: re-run `load_hooks_config(workspace, trust_project=True)`
  (`hooks/config.py`), swap the config on the `HookRunner`.
- **MCP**: `load_mcp_config(trust_project=True)` (`mcp/config.py`), register
  the project servers with the manager and connect them (existing
  `enable_server`/`_connect_one` machinery in `mcp/manager.py`).
- **LSP**: rebuild the `LspRegistry` with project/plugin providers
  (`build_lsp_registry(trust_project=True)`); the manager connects lazily so
  no forced restart.
- Idempotent: calling when already trusted is a no-op.

**Revoke** is persist-only + a warning that already-running hook/MCP/LSP
processes stop on restart. `TrustState` flips immediately so lazy readers
(skills/agents/instructions) drop project content on the next turn.

### 5. `/trust` command + settings row (TUI)

- `/trust` — status: trusted/untrusted, decision source (env / store /
  config / unset), surface summary, fingerprint freshness.
- `/trust on` — persist + `apply_project_trust()` (same path as the panel).
- `/trust off` — persist + restart warning.
- Settings screen: the read-only "Trust project hooks" row
  (`interfaces/tui/settings.py:502`) upgrades to show state + source.

### 6. CLI: `marim trust` subcommand

New module under `interfaces/cli/` on the router, lazily imported like
`config`/`models`:

- `marim trust` — status + surface summary for the workspace (cwd or arg).
- `marim trust grant` — persist trusted with the *current* fingerprint.
- `marim trust revoke` — persist untrusted.

Headless `-p` runs never prompt; when the surface is non-empty and the
project is untrusted, print **one stderr line** (never in model output /
json): `project config present but not trusted; run 'marim trust grant' or
set MARIM_TRUST_PROJECT_HOOKS=1`. No `--trust-project` one-shot flag in this
iteration (`marim trust grant && marim -p …` covers it).

### 7. Serve API

Workspace-level (trust is a property of the daemon's workspace, not a
session):

- `GET /v1/trust` → `{trusted, source, fingerprint_fresh, surface: {...}}` —
  everything a remote client needs to render its own trust dialog.
  `Cache-Control: no-cache` (decision state, like `get_session`).
- `POST /v1/trust` `{"trusted": true|false}` → persist against the *current*
  fingerprint, then hot-apply to live sessions through the same
  `apply_project_trust()` seam (revoke: flip `TrustState` + report the
  restart caveat in the response).
- Session payloads / session-create response grow `trust_prompt_pending`
  (mirrors the TUI's mount check) so clients know to show the dialog.

**Honest limit (documented, not mitigated here):** `POST /v1/trust` lets a
remote client enable startup code execution. Serve already exposes turn
execution (bash in auto mode) to whoever can reach it, so trust adds no new
exposure class — one sentence in `docs/guides/trust.md` and
`docs/reference/serve-api.md` says exactly that.

### 8. Error handling

- Store I/O failure on **read** ⇒ empty store, warning log, fail closed.
- Store I/O failure on **write** (grant/revoke) ⇒ surface the error to the
  user (panel/command/API 500); trust state still flips for this session on
  grant (the user consented; only persistence failed).
- Fingerprint computation failure ⇒ empty-section fallback (see §1).
- `apply_project_trust()` partial failure (an MCP server fails to connect)
  ⇒ same as startup behavior: failure recorded per server, rest proceeds.

### 9. What does NOT change

- The env var name, its blocklist entry (project `.env` cannot self-trust),
  and the fail-closed default.
- Per-plugin executable trust bits (`plugins/install.py`) — project trust
  remains the outer gate; plugin bits remain the inner gate.
- The claude-cli main-loop provider (marim's loaders don't apply there
  beyond what already loads today).
- No new auth layer on serve.

### 10. Testing

- **Pure:** store round-trip, corrupt file, path-keying (entry for A never
  trusts B), fingerprint match/mismatch, tri-state resolution order
  (explicit > env > store > untrusted; falsy env forces untrusted over a
  trusting store), surface summary builder on fixture workspaces.
- **TUI (Pilot):** panel mounts on first open with surface present + no
  decision; no panel when surface empty / decision stored / env set; grant
  persists + closes + confirmation; decline persists + notice; `/trust`
  status/on/off.
- **Reload:** harness-level — after `apply_project_trust()`, a project
  skill is discoverable and a project MCP server connectable without a
  rebuild; hooks config swap visible to the runner.
- **Headless/serve:** stderr notice line exactly once; `GET /v1/trust`
  shape; `POST /v1/trust` persists + applies; `trust_prompt_pending` in
  session payloads; stored TUI decision honored by `-p` run.
- **Security-shaped regressions:** existing blocklist tests keep passing;
  stale fingerprint ⇒ re-prompt path; unanswered panel ⇒ untrusted turn.

## Out of scope

- Per-category trust tiers; `--trust-project` CLI flag; serve auth changes;
  killing live processes on revoke; any change to plugin-level trust UX.
