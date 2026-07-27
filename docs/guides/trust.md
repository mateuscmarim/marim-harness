# Trust and permissions

marim is a coding agent: it edits files, runs shell commands, and can load
project-local configuration. This guide explains the layers that decide what it
may do — permission modes, the shell command policy, file-tool path guards, and
the project trust gate — and, just as importantly, what those layers do *not*
protect you from. For the short version and how to report a vulnerability, see
[SECURITY.md](../../SECURITY.md).

The one-paragraph summary: gated tools go through an approval flow driven by the
current **mode** (`auto` / `ask` / `plan`); the `bash` tool additionally honors a
regex **command policy**; file tools are **path-confined** to the workspace (plus
the session scratchpad); and anything a cloned repo could use to run code on
startup — project hooks, project MCP servers, project plugins, third-party LSP
manifests, project skills/agents — loads only behind the
**`MARIM_TRUST_PROJECT_HOOKS`** gate.

## Permission modes

Three modes (`src/marim_harness/runtime/permissions.py`): `ask` (the default),
`auto`, and `plan`. A mode only matters for tools registered as *gated* —
ungated tools (local reads like `read_file`, `grep`, `tree`, `glob`, the LSP
navigation tools, `remember`/`recall`, skills, task tools) run in every mode.

### Which tools are gated

The mutating core is the `GATED_TOOLS` set in `src/marim_harness/tools/names.py`:

- `write_file`
- `edit_file`
- `bash`

On the main agent, a few more tools are registered behind the same approval flow
(`requires_approval=True` in `src/marim_harness/tools/provider.py` and
`tools/forge_tools.py`):

- `web_search` and `fetch_url` — outbound network is an exfiltration boundary,
  so it is gated like a mutation even though it doesn't touch the workspace
- `forget` — the only irreversible memory operation
- `run_workflow` — executes a model-authored script (when the workflows extra
  is enabled)
- `create_pr` and `checkout_pr` — the two mutating forge tools

MCP server tools are gated separately, per server — see
[MCP servers in ask mode](#approval-ux-quick-pointers) below.

### What each mode does with a gated call

- **`auto`** — every gated call is approved and runs without prompting. Use it
  only when you are comfortable with unattended writes and shell commands in
  this workspace.
- **`ask`** — every gated call pauses the turn and prompts you in the TUI
  (an inline approval panel; approve or deny per call). Two carve-outs:
  `write_file`/`edit_file` targeting the session scratchpad are auto-approved
  (that directory exists precisely so intermediate files don't prompt; `bash`
  never qualifies), and when no approver is wired — a non-interactive run —
  gated calls are denied rather than silently run ("no approver available;
  denied"). That is why headless `--mode` only offers `plan` and `auto`:
  `ask` needs the TUI.
- **`plan`** — read-only. Gated calls are denied, with two refinements
  (`_plan_decision` in `runtime/permissions.py`):
  - `bash` is allowed **only** when the command classifies as read-only
    (`src/marim_harness/read_only_commands.py`): a single command with no shell
    metacharacters (no `;`, `&`, `|`, redirection, backticks, `$(...)`),
    from a conservative roster (`ls`, `cat`, `grep`, …) or passing a
    per-program argument screen (`git status/log/diff/…`, `find`/`fd`/`rg`
    without exec flags, and similar). Anything else is denied with
    "plan mode: read-only commands only".
  - `web_search`/`fetch_url` are denied too: plan mode is *local* research
    only, because an injected fetch URL or search query could carry file
    contents off the host with zero approval.
  - `write_file`/`edit_file` (and `forget`, `run_workflow`, the mutating forge
    tools) are simply denied ("read-only plan mode"). MCP tool calls are also
    denied in plan mode by each server's approval hook.

  The read-only classifier is a best-effort nudge, **not a sandbox** — see
  [What trust does not cover](#what-trust-does-not-cover).

### Switching modes

- `ctrl+t` in the TUI cycles `ask → auto → plan`.
- `/mode ask|auto|plan` sets a mode directly.
- Headless: `marim -p ... --mode plan|auto` (defaults to `auto`).
- `MARIM_DEFAULT_MODE` sets the startup mode for interactive sessions
  (default `ask`).

`MARIM_DEFAULT_MODE` is honored **only from your real shell environment or the
global config** (`~/.config/marim/.env`) — never from a project-local `.env`.
A cloned repo shipping `MARIM_DEFAULT_MODE=auto` in its `.env` would silently
make your sessions auto-approve every command in that repo, so the key is on the
project-env blocklist (`_PROJECT_ENV_BLOCKLIST` in
`src/marim_harness/config/env.py`), together with the trust flag, the command
allow/deny lists, and the provider/endpoint/credential keys. A repository must
not be able to elevate its own permissions.

## The command policy

`MARIM_COMMAND_DENYLIST` and `MARIM_COMMAND_ALLOWLIST`
(`src/marim_harness/command_policy.py`) add allow/deny rules for the `bash`
tool, on top of the mode gating:

- Each value is a comma- or newline-separated list of **regular expressions**,
  matched with `re.search` against the whole command string. Because `re.search`
  finds a match anywhere, a plain literal substring works as a pattern with no
  regex knowledge: `MARIM_COMMAND_DENYLIST='rm -rf,git push --force'`. A pattern
  that needs a literal comma can use a character class (`[,]`).
- **Deny takes precedence over allow.** A command is blocked when it matches any
  denylist pattern, or — when an allowlist is configured — when it matches none
  of the allowlist patterns.
- **The built-in policy is empty.** marim ships no built-in denied commands: an
  empty policy allows everything, and the CLI builds the policy purely from the
  two environment variables (`runtime/bootstrap.py`). What you configure is the
  whole policy.
- A malformed regex **fails closed**, loudly: a broken deny rule blocks every
  command, a broken allow rule grants nothing, and a warning is logged either
  way — a broken rule never silently degrades into no protection.

The policy is enforced *inside* the `bash` tool itself
(`src/marim_harness/tools/edit_tools.py`), not at the approval prompt. That
placement is the point: it applies uniformly in **every mode** — including
`auto`, which skips approval prompts entirely — and to sub-agents, which run
their granted tools without prompting. In `ask` mode the order is approval
first, then policy: you can approve a command and still see it come back
"Blocked by command policy: …".

Like the read-only classifier, this is defense-in-depth, **not a sandbox**: the
pattern matches the raw string while `bash` runs a real shell, so quoting,
`$(...)`, `eval`, and pipes can all evade a pattern. Use it to catch honest
mistakes and steer the model, not to contain a hostile command.

Both variables are on the project-env blocklist: a project `.env` cannot rewrite
your command policy.

## Path guards

The file tools (`read_file`, `write_file`, `edit_file`, `glob`, `tree`, `grep`)
are confined to the workspace root. Every path is resolved — chasing `..` and
symlinks — and must land inside the root (`resolve_in_workspace` in
`src/marim_harness/workspace/fs.py`); the guard checks the *real* target, not a
string prefix. The traversal tools hold the same line from the other direction:
`tree` never descends a symlinked directory, `grep` skips symlinked files that
escape the root, and `glob` drops matches that resolve outside it.

Two deliberate widenings, one per direction
(`src/marim_harness/tools/impl/fs.py`):

- **Extra read roots** — skill directories that live outside the workspace are
  readable (read-only) so an activated skill's files can be opened.
- **Extra write root: the session scratchpad** — a per-session temp directory
  (`/tmp/marim-<uid>/<workspace>-<hash>/<session-id>/scratchpad`,
  `src/marim_harness/workspace/scratchpad.py`) for intermediate artifacts that
  shouldn't pollute the project tree. It is created `0700`, refused (and the
  feature disabled) if the base directory is a symlink or owned by another user
  (classic `/tmp` squatting), and gated by `MARIM_SCRATCHPAD` (default on).
  The workspace root is always tried first, so a relative path can never be
  captured by the scratchpad — only an absolute path genuinely inside it.

**On an out-of-root path** the tool call fails with a retryable, model-facing
error — `path outside workspace: <path>` — before any filesystem access. The
model sees the message and can correct itself; nothing outside the root is read
or written by these tools. (The `bash` tool is *not* path-confined — see the
honest limits below.)

## The project trust gate

A cloned repository can ship configuration that launches processes on startup or
injects text into the model's context — before any tool-call approval could
apply. All of it is **off by default** and loads only when the project is
trusted. Trust is a **per-project, persistent decision** — remembered on your
machine after the first answer — with four ways to make it: the TUI's
first-open prompt, the `/trust` command, the `marim trust` CLI, and (for
`marim serve`) a REST endpoint. All four write the same store and drive the
same reload; none of them are required — the env var below still works as a
standalone override, for scripts and CI that never touch the store.

### The store and resolution order

Decisions live in `$XDG_STATE_HOME/marim-harness/trusted-projects.json`
(`~/.local/state/marim-harness/` when `XDG_STATE_HOME` is unset) — machine
state, never inside the repo, keyed by the **resolved** workspace root:

```json
{
  "/abs/resolved/workspace/root": {
    "trusted": true,
    "fingerprint": "<canonical surface JSON>",
    "decided_at": "2026-07-26T21:00:00Z"
  }
}
```

Both answers are remembered — a decline persists too, so an untrusted project
shows a one-line notice instead of re-prompting every time. A stored decision
is honored only while `fingerprint` still matches the project's current
*executable* surface (resolved `.marim/hooks.json` entries, `.marim/mcp.json`
server specs, and each project-scope plugin's executable surface — the same
shape as the plugin registry's own fingerprint). Skills/agents text is
deliberately excluded from the fingerprint, matching the plugin-trust policy:
editing a skill or adding an agent spec must not silently drop trust. When the
surface has changed since the decision was recorded, the stored entry is
treated as absent — the TUI re-prompts, headless runs stay untrusted. A
corrupt or unreadable store reads as empty (fail closed, warning logged).

Full resolution order, checked in this sequence
(`trust.py::resolve_project_trust`):

1. **Explicit config** — a value threaded in by the embedding caller (rare;
   wins unconditionally).
2. **`MARIM_TRUST_PROJECT_HOOKS`** — set (any truthy spelling: `1`/`true`/
   `on`/`yes`) forces trusted; set to anything else (`0`, `false`, ...) forces
   **untrusted, even over a trusting store entry**. Only an *unset* variable
   falls through to the store.
3. **The store**, only while fingerprint-fresh (above).
4. **Untrusted** — the fail-closed default.

That means the env var is still a full override in both directions: it can
force trust on for a repo you haven't clicked through, or force it off even
after you granted it interactively (e.g. a CI job that must never trust
anything, regardless of what a developer's laptop has stored).

### TUI: the first-open prompt, `/trust`, and the settings row

When a workspace has a non-empty gated surface and no usable decision (env
unset, store empty or stale), the harness starts the session **untrusted**
and the TUI mounts an inline `TrustPanel` above the status bar on open (never
a modal — the transcript stays scrollable and typing a prompt is never
blocked):

```
This project ships configuration that loads on startup:
  hooks: 2 (SessionStart, PreToolUse) · mcp: 1 (docs-server) · skills: 3 · agents: 1
Trust it? Hooks and MCP servers run code with no per-call approval; skills
and agents inject prompt content. docs/guides/trust.md
[t] Trust   [d] Don't trust
```

- **`t` / Trust** — persists `{trusted: true, fingerprint}` and hot-applies it
  immediately: hooks config reloads, project MCP servers connect, the LSP
  registry rebuilds to include project/plugin providers. A failure partway
  through (say, one MCP server won't connect) is reported inline rather than
  silently swallowed; the rest of the apply still proceeds.
- **`d` / Escape / Don't trust** — persists `{trusted: false, fingerprint}`;
  a notice appears instead (`Project config present but not trusted — /trust
  to enable.`).
- An unanswered panel means **untrusted for this session** — the panel stays
  mounted (fail closed) rather than the turn quietly running trusted
  underneath it.

`/trust` (bare) reports the resolved state, the source that decided it
(`config`/`env`/`store`/`default`), and the gated surface. `/trust on` grants
— same persist-then-hot-apply path as the panel. `/trust off` revokes:
persists immediately, but warns that already-running MCP/LSP processes for
this project keep running until the app restarts (nothing kills a live
subprocess on revoke). The Settings screen's "Trust project hooks" row shows
the live state and its source, not just the env var.

### CLI: `marim trust`

```bash
marim trust                 # status: decision + source + gated surface (cwd)
marim trust status <path>   # same, for another workspace
marim trust grant [<path>]  # persist trusted, against the current fingerprint
marim trust revoke [<path>] # persist untrusted
```

`status` is the default action; the workspace defaults to the current
directory; a bad path exits `2`. `marim trust` is intentionally cheap — it
stays off the `pydantic_ai` import path (like `marim config`/`marim models`),
so checking or flipping trust from a script or CI step doesn't pay for
loading the agent stack.

Headless (`marim -p ...`) never prompts — there's no one to answer. When the
workspace has a non-empty gated surface and no usable trust decision, it
prints one line to **stderr only** (never stdout, never the JSON/NDJSON
result):

```
note: project config present but not trusted; run 'marim trust grant' or set MARIM_TRUST_PROJECT_HOOKS=1
```

`marim trust grant && marim -p ...` is the one-shot pattern; there is no
`--trust-project` flag on `-p` itself.

### `marim serve`

Trust is a property of the daemon's **workspace** (its directory on disk),
shared by every session on it. `GET /v1/workspaces/{ws}/trust` and
`POST /v1/workspaces/{ws}/trust` expose the same store/resolve/apply seam over
HTTP — grant hot-applies to every live session on that workspace, revoke
flips state and warns about the restart caveat above — and session payloads
carry `trust_prompt_pending` so a remote client knows when to show its own
dialog. Full request/response shapes: [serve API
reference](../reference/serve-api.md#trust). The same honest limit applies
there as to the rest of serve: a client that can call `POST .../trust` can
enable startup code execution, but serve already exposes turn execution
(`bash` in auto mode) to whoever can reach it, so this adds no new exposure
class.

What the gate covers (each verified at its loader):

- **Project hooks** — `.marim/hooks.json` runs arbitrary commands on lifecycle
  events (`hooks/config.py`); see [the hooks guide](hooks.md).
- **Project MCP servers** — `.marim/mcp.json` servers launch subprocesses or
  connect to endpoints at connect time (`mcp/config.py`); see
  [the MCP guide](mcp.md). An untrusted project's file is never loaded, and
  runtime enable/disable toggles are never persisted into it either.
- **Project-scope plugins** (`.marim/plugins/`) — their *inert* content
  (skills, agents, `AGENTS.md` text) requires project trust; their *executable*
  surface (hooks, MCP, LSP) additionally requires the per-plugin `trusted` bit
  (`plugins/discovery.py`). The plugin registry travels with the repo, so its
  own bits cannot be taken at face value on a fresh clone.
- **Third-party LSP providers from plugins** — an LSP manifest block launches a
  language server on connect, so it follows the same enabled+trusted rule as
  plugin MCP (`plugin_lsp_providers` in `plugins/discovery.py`).
- **Project-local skills** — `.marim/skills/` (`workspace/skills.py`). Skills
  don't execute code, but their text is injected into the model's context on
  activation: a prompt-injection channel, gated the same way. See
  [skills and memory](skills-and-memory.md).
- **Project-local sub-agent specs** — `.marim/agents/` (`workspace/agents.py`),
  for the same reason: a spec shapes a sub-agent's prompt, reach, and model.
  See [the sub-agents guide](subagents.md).

What the gate does **not** cover — these always load, because they come from
your machine or the package, not the repo:

- Your real shell environment and the global config directory
  (`~/.config/marim/`): global `.env`, global `hooks.json`, global `mcp.json`,
  global skills/agents, global-scope plugins, and user-level `AGENTS.md`.
- The four bundled LSP language plugins (python, typescript, cpp, java) that
  ship inside the package. marim never downloads server binaries — it only
  probes `PATH` for ones you installed.
- The project's own `AGENTS.md` / `CLAUDE.md` instructions file, and of course
  the repo's source files read by the tools. Reading the repo is the product;
  be aware that repo text reaching the model is inherently untrusted input.

Treat trusting a project as a **supply-chain decision**, not a convenience
toggle: everything behind it runs (or is injected) with no per-call approval,
on startup, from files whoever authored the repo controls. Decline (or leave
`MARIM_TRUST_PROJECT_HOOKS` unset) for repositories you don't fully control.
A project `.env` cannot set the env var (blocklist, see above), so a repo
cannot self-trust — and the `XDG_CONFIG_HOME`/`XDG_DATA_HOME` redirect that
would let a repo substitute its own "global" config, or point `$XDG_STATE_HOME`
at a repo-controlled trust store, is blocked from project `.env` files too.

## Approval UX quick pointers

- **Scratchpad writes don't prompt** in ask mode — intermediate files are
  pre-blessed by design; see [Path guards](#path-guards).
- **MCP servers** honor a per-server `"trust": true` flag in `mcp.json`: in ask
  mode a trusted server's tool calls run without prompting, an untrusted one
  prompts per call (and all MCP calls are denied in plan mode). Sub-agents
  spawned in ask mode are simply not granted servers that would prompt.
  Full detail: [the MCP guide](mcp.md).
- **Forge tools**: only `create_pr` and `checkout_pr` are approval-gated; the
  read-only forge tools (list/view/CI status) are not. Master switch:
  `MARIM_FORGE`.
- **TUI approval panels** (inline, above the status bar): see
  [the TUI guide](tui.md).
- **Plugins and trust bits**: `docs/plugins.md`; third-party LSP:
  `docs/lsp-plugins.md`.

## What trust does not cover

Honest limits — read these before pointing marim at anything sensitive:

- **`auto` mode runs commands unattended.** There is no prompt between the
  model deciding to run a command and the command running. The command policy
  and the plan-mode classifier are regex nudges over a string handed to a real
  shell — trivially evadable, and documented as such in their own source.
- **`bash` is not path-confined.** The file-tool path guards bound the file
  tools only; a shell command can read or write anywhere the marim process can.
- **The model can read any secret the process can read.** Environment
  variables, `~/.ssh`, dotfiles, tokens on disk — via `bash` — and the gated
  network tools mean an approved (or auto-mode) fetch can carry data off the
  host. Repo contents are untrusted model input, so prompt injection is part of
  the threat model, not a hypothetical.
- **Recommended posture for unfamiliar repos:** leave `MARIM_TRUST_PROJECT_HOOKS`
  unset, start in `plan` mode to look around, move to `ask` to work, and reserve
  `auto` for repos and tasks you'd be comfortable running a script from. For
  real isolation, run marim inside a container/VM — none of the layers above is
  a sandbox, and none claims to be.

If you find a way for a repository's contents to run code without the trust
gate, or for a gated tool to run without approval in `ask`/`plan` mode, that is
exactly what [SECURITY.md](../../SECURITY.md) asks you to report.
