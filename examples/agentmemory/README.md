# agentmemory

Persistent agent memory for marim: auto-capture across the session lifecycle
plus `memory_*` recall/save MCP tools. marim's hook engine speaks Claude Code's
hook contract, so agentmemory's bundled hook scripts run unmodified.

This is a **wiring-only plugin**. It does not vendor agentmemory's code — the
hook scripts and MCP server live in agentmemory's own npm distribution. The
plugin only tells marim how to invoke them, so the external install, a running
server, and the environment below are still prerequisites.

## Prerequisites

1. Run the agentmemory server and note its URL (default `http://localhost:3111`).
2. Install agentmemory and point `CLAUDE_PLUGIN_ROOT` at its plugin directory,
   then **export it in the environment that launches marim** (add it to your
   shell profile — a value set in one terminal won't be seen by a marim started
   elsewhere):
   ```bash
   export CLAUDE_PLUGIN_ROOT="$(npm root -g)/@agentmemory/agentmemory/plugin"
   ```
   The hook commands are run through a shell, so `${CLAUDE_PLUGIN_ROOT}` expands
   from this environment at fire time. Confirm the scripts resolve:
   ```bash
   ls "$CLAUDE_PLUGIN_ROOT/scripts"   # session-start.mjs, prompt-submit.mjs, ...
   ```

## Install

    marim plugin install examples/agentmemory --scope global --trust

The plugin bundles hooks **and** an MCP server (executable surface), so install
prompts for trust. Install it to **global** scope — never as a project plugin.
A project-local plugin's hooks/MCP launch code from a cloned repo and are gated
behind `MARIM_TRUST_PROJECT_HOOKS` precisely because that is a supply-chain risk;
agentmemory is your own tooling and belongs in the global scope.

After install, `/mcp` shows the `agentmemory_agentmemory` server and the nine
lifecycle hooks fire automatically — `SessionStart`/`UserPromptSubmit` inject
recalled context, the tool hooks observe the turn, and the teardown hooks flush
memory.

## Configuring the server URL and secret

marim passes an MCP server's `env` block to the child process **verbatim — it
does not expand `${VAR}` references** (the child otherwise inherits only a safe
default subset of the environment, which excludes `AGENTMEMORY_*`). So the
committed manifest hardcodes the local default `AGENTMEMORY_URL` and carries **no
secret** (a secret must never be committed).

If your server needs a different URL or a secret, edit the **installed** copy's
manifest — `~/.config/marim/plugins/agentmemory/.marim-plugin/plugin.json` — and
put **literal** values under `mcpServers.agentmemory.env`, e.g.:

```json
"env": {
  "AGENTMEMORY_URL": "http://your-host:3111",
  "AGENTMEMORY_SECRET": "the-actual-secret"
}
```

Because that file lives outside the repo, the literal secret is never committed.
Placeholder syntax like `"${AGENTMEMORY_SECRET}"` will **not** work here — it
would reach the server as the literal string.

## Verify against your install

Script names, the npm package (`@agentmemory/mcp`), and the default port (`3111`)
reflect agentmemory `0.9.27`. Confirm them against your version:

```bash
ls "$CLAUDE_PLUGIN_ROOT/scripts"
npm view @agentmemory/mcp version
```

The nine wired events (`SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, `PreCompact`, `SubagentStart`, `SubagentStop`, `Stop`,
`SessionEnd`) are the full set marim's hook engine supports; agentmemory ships a
script for each.
