# MCP servers

marim can connect to [Model Context Protocol](https://modelcontextprotocol.io/)
servers and expose their tools to the model alongside the built-in tools. This
guide covers configuring servers, the `marim mcp` CLI, the connection lifecycle,
trust and approval, how MCP tools reach the model (including sub-agents and
tool-search deferral), and a worked example.

## Config files and scopes

Servers are declared in two JSON files, merged at startup:

- **Global** — `~/.config/marim/mcp.json` (strictly: `$XDG_CONFIG_HOME/marim/`
  falling back to `~/.config/marim/`). Always loaded.
- **Project** — `.marim/mcp.json` in the workspace. Loaded **only when the
  project is trusted** (`MARIM_TRUST_PROJECT_HOOKS=1` — see
  [Trust](#trust-and-approval) below).

Merge precedence, lowest first: servers from enabled+trusted plugins
(namespaced `<plugin>_<server>`), then global, then project — so on a name
clash your project entry wins over a global one, and both win over a plugin's.
A missing or malformed file is treated as empty, never fatal.

The shape is Claude-Code-compatible: a top-level `mcpServers` object mapping a
server name to its spec.

```json
{
  "mcpServers": {
    "files": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-fs"] },
    "web":   { "url": "https://example.com/mcp" },
    "events": { "url": "https://example.com/sse", "type": "sse" },
    "docs":  { "command": "node", "args": ["server.js"], "trust": true }
  }
}
```

Accepted keys per spec:

| Key       | Applies to | Meaning                                                        |
|-----------|------------|----------------------------------------------------------------|
| `command` | stdio      | Executable to launch. Presence of this key selects stdio.      |
| `args`    | stdio      | Argument list (values are stringified).                        |
| `env`     | stdio      | Environment variables for the child process.                   |
| `cwd`     | stdio      | Working directory for the child process.                       |
| `url`     | http/sse   | Endpoint URL. Presence of this key (no `command`) selects HTTP.|
| `headers` | http/sse   | HTTP headers (e.g. an `Authorization` bearer token).           |
| `type`    | http/sse   | `"sse"` selects the SSE transport; anything else (or absent) uses streamable HTTP. |
| `trust`   | both       | `true` skips the per-call approval prompt in ask mode.         |
| `enabled` | both       | Only an explicit `false` disables the server (it is still listed and can be re-enabled live). |

A spec that is not an object, or that has neither `command` nor `url`, is
skipped with a warning — one bad entry never takes down the rest.

Stdio servers' stderr is captured to `~/.config/marim/mcp-stderr.log` so
startup banners don't print over the TUI.

## The `marim mcp` CLI

`marim mcp` edits the two config files without hand-editing; the flag surface
mirrors `claude mcp add`, and hand-editing produces identical results.

```bash
# Add a stdio server (the default transport); -e sets child env vars
marim mcp add mddocs node /path/dist/index.js -e MDDOCS_API_KEY=xxx

# Add an HTTP server; -H sets request headers
marim mcp add --transport http mddocs https://nanocore.marim.dev/mcp \
    -H "Authorization: Bearer mddocs_xxx"

marim mcp list              # all servers from both scopes: name, [source], target
marim mcp list --json       # same, as JSON with a "source" field per server
marim mcp get mddocs        # one entry, as JSON
marim mcp remove mddocs     # delete (searches project scope, then user)
```

Flags on `add`:

- `-t`, `--transport stdio|http|sse` — default `stdio`.
- `-s`, `--scope user|project` — `user` targets the global file, `project`
  (the default) targets `.marim/mcp.json`. Adding to project scope prints a
  reminder that project servers only load under `MARIM_TRUST_PROJECT_HOOKS`.
- `-H`, `--header "NAME: VALUE"` — repeatable; http/sse only (an error on stdio).
- `-e`, `--env KEY=VALUE` — repeatable; stdio only (an error on http/sse).
- `--trust` — writes `"trust": true` into the spec (bypasses ask-mode approval
  for this server's tool calls; see below).
- `-C`, `--workspace DIR` — choose the workspace root for project-scoped
  operations (like `git -C`).

`add` refuses to overwrite an existing name — `remove` it first or pick another
name. `remove` accepts `-s`/`--scope` to limit which file it touches; without
it, project is searched before user.

Because the command and its arguments are a positional remainder, child
arguments that start with `-` can be swallowed by marim's own flag parsing.
Put `--` before the command to stop flag parsing:

```bash
marim mcp add srv -- node --inspect server.js
```

## Lifecycle

Servers are built when the harness starts and connected once per session: the
TUI connects on mount, and a headless run connects before executing the prompt.
Connections open **concurrently**, so startup latency is the slowest server,
not the sum.

A server that fails to connect is never fatal. Its failure is recorded and
surfaced — the TUI prints an `MCP <name> failed: <error>` notice next to the
`MCP connected: ...` line — and the session runs fine with whichever servers
did come up (or none). On exit, connections are closed and stdio child
processes are reaped deterministically.

### The `/mcp` command

Inside the TUI:

- `/mcp` — list every configured server with its live state: `connected`,
  `failed` (with the error), `disabled`, or `not connected`.
- `/mcp disable <name|all>` — stop routing a server's tools to the model.
- `/mcp enable <name|all>` — re-enable, connecting the server on the spot if it
  isn't live yet (a connect error is reported inline).

Toggles persist: the `enabled` flag is written back into whichever config file
defines the server (project preferred, when trusted), so a disabled server
stays disabled across restarts. A server disabled in the file
(`"enabled": false`) is still built and listed — just never launched — so it
remains one `/mcp enable` away. The Settings screen offers the same toggles.

## Trust and approval

Two separate trust decisions apply.

**Project-scope loading.** Servers in `.marim/mcp.json` launch subprocesses or
connect to endpoints *at connect time*, before any tool-call approval gate can
apply — so a cloned, untrusted repository shipping that file could otherwise
run arbitrary commands on first launch. Project servers (and MCP servers from
project-scope plugins) therefore load only when `MARIM_TRUST_PROJECT_HOOKS=1`,
the same gate as project-local hooks. Global servers and global plugins are
your own configuration and always load.

**Per-call approval.** Every config-built server's tool calls pass through a
gate that reads the live session mode at call time:

- **plan** — every MCP call is denied (read-only mode), trust flag or not.
- **auto** — every MCP call runs.
- **ask** — a server with `"trust": true` runs its calls without prompting;
  an untrusted server raises a per-call approval prompt (shown as
  `<server>_<tool>` with the arguments). A rejected call — or one made where no
  approval UI exists — returns a denial string to the model instead of running.

That is exactly what the `trust` flag changes: ask-mode behavior. It has no
effect in auto (already unprompted) or plan (always denied). It has one
knock-on effect on sub-agents, described next.

## How MCP tools reach the model

Each server's tools appear to the model prefixed with the server's config name:
`<server>_<tool>` (e.g. `mddocs_search`). A server that publishes usage
`instructions` gets them injected into the conversation once, when its tools
are first discovered, capped at 2000 characters per server. Oversized tool results (a third-party server has no inherent
size bound) are offloaded to a workspace file and replaced with a handle plus
preview, so one huge response can't flood the context.

### Tool-search deferral

A large MCP surface would otherwise put every tool schema in every request.
marim can defer the MCP tool surface behind Pydantic AI's native tool search:

- `MARIM_TOOL_SEARCH` — `off` (always load all MCP tools inline), `on` (always
  defer), or `auto` (the default: defer only when the live MCP tool count
  exceeds the threshold).
- `MARIM_TOOL_SEARCH_THRESHOLD` — the `auto` cutoff; default `15`. Deferral
  fires when the count strictly exceeds it.

When deferral is on, the model sees a `search_tools` function instead of the
full schemas, plus a prompt catalog listing deferred tool names grouped by
server (at most 12 names per server, then `(+N more)`). It discovers and loads
individual tools by keyword on demand, so the loaded surface stays proportional
to what the turn needs. Built-in tools are never deferred. When LSP navigation
tools are enabled they share the same budget and defer together with MCP.

### Granting servers to sub-agents

Sub-agents get **no MCP servers by default**. The main agent grants them
per-spawn via `spawn_agent`'s `mcp` argument — a list of server names, e.g.
`mcp=["mddocs"]` — and the prompt carries an index of the enabled server names
it may grant. Unknown or disabled names are ignored and noted in the spawn
report. There is no `mcp:` key in agent spec frontmatter — a custom agent file
narrows built-in tools via `tools:`, but MCP reach is decided at spawn time by
the spawner.

A spawn's reach is fixed up front (sub-agents never prompt mid-run), so the
session mode filters the grant:

- **auto** — the full requested grant; calls run unprompted.
- **ask** — only servers marked `"trust": true` are granted; an untrusted
  server would need a per-call prompt the spawn cannot raise, so it is withheld
  (and the withholding is noted).
- **plan** — the whole grant is withheld.

The granted subset gets the same tool-search deferral decision as the main
agent, computed over just the granted servers' tool count — a big grant is
searchable, not dumped wholesale into the spawn's context.

## Worked example

Add a stdio documentation server to the global scope, trusted so ask mode
doesn't prompt on every lookup:

```bash
marim mcp add -s user --trust mddocs node /opt/mddocs/dist/index.js \
    -e MDDOCS_API_KEY=xxx
```

Add a remote HTTP server to the project (requires a trusted project to load):

```bash
export MARIM_TRUST_PROJECT_HOOKS=1
marim mcp add --transport http issues https://forge.example.com/mcp \
    -H "Authorization: Bearer tok_xxx"
```

The resulting `.marim/mcp.json`:

```json
{
  "mcpServers": {
    "issues": {
      "url": "https://forge.example.com/mcp",
      "headers": { "Authorization": "Bearer tok_xxx" }
    }
  }
}
```

Start `marim`, confirm both connected with `/mcp`, and the model now sees
`mddocs_*` and `issues_*` tools (or a searchable catalog of them, if the count
crossed the tool-search threshold).

To put a server in a sub-agent's hands, ask the main agent to delegate with a
grant — it calls `spawn_agent` with the server name:

```text
spawn_agent(type="explore",
            task="Find the three issues most related to the failing test",
            mcp=["issues"])
```

The sub-agent runs with the `issues_*` tools added to its toolset, gated by the
same mode rules as the main agent's own MCP calls. Since `issues` was added
without `--trust`, that grant goes through in auto mode but is withheld in ask
mode — mark the server trusted if sub-agents should use it there.
