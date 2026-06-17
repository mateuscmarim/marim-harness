# agentmemory integration

marim's hook engine speaks Claude Code's hook contract, so agentmemory's bundled
hook scripts run unmodified. Two layers:

1. **MCP (`memory_*` tools)** — merge `mcp.json` into `~/.config/marim/mcp.json`.
   Gives the model agentmemory's tools on demand. Keep this in the **global**
   config (a project `.marim/mcp.json` from a cloned repo is a supply-chain risk).
2. **Hooks (auto-capture + recall)** — copy `hooks.json` to
   `~/.config/marim/hooks.json`. `SessionStart` injects recalled context;
   `UserPromptSubmit`/`PostToolUse` observe the turn.

## Setup

1. Run the agentmemory server (default `http://localhost:3111`) and export
   `AGENTMEMORY_SECRET` in your environment (never commit it).
2. Set `CLAUDE_PLUGIN_ROOT` to agentmemory's plugin directory, e.g.:
   ```bash
   export CLAUDE_PLUGIN_ROOT="$(npm root -g)/@agentmemory/agentmemory/plugin"
   ```
   The hook commands resolve `${CLAUDE_PLUGIN_ROOT}` at fire time, so this must
   be set in the environment that launches marim. Add the `export` to your shell
   profile (`~/.zshrc`, `~/.bashrc`, …) so it persists across sessions — a value
   set only in one terminal won't be seen by a marim started later or elsewhere.
3. Copy `hooks.json` to `~/.config/marim/hooks.json` and merge `mcp.json`
   into `~/.config/marim/mcp.json`.
4. Start marim. `/mcp` shows the `agentmemory` server; hooks fire automatically.

## Trust

Global hooks (`~/.config/marim/hooks.json`) always run. Project-local
`.marim/hooks.json` files are **ignored** unless you explicitly set
`MARIM_TRUST_PROJECT_HOOKS=1`. Project hooks are a supply-chain risk: a cloned
repository could contain a `.marim/hooks.json` that auto-launches arbitrary
commands on your machine. Only enable `MARIM_TRUST_PROJECT_HOOKS` in repositories
you control and trust. The recommended install location for agentmemory is the
**global** config, not a project file.

## Verify against your install

agentmemory's hook scripts are **`.mjs` Node.js ES modules**, not shell scripts.
The task brief's references to `session-start.sh` / `observe.sh` are inaccurate —
the real scripts are `session-start.mjs`, `prompt-submit.mjs`, `pre-tool-use.mjs`,
`post-tool-use.mjs`, etc., invoked via `node`.

Script names, the npm package (`@agentmemory/mcp`), and the port (`3111`) reflect
agentmemory at the time of writing (package version `0.9.27`). Confirm them against
your installed version:

```bash
ls "$(npm root -g)"/@agentmemory/agentmemory/plugin/scripts
npm view @agentmemory/mcp version
```

## Deviation from task brief

The brief listed `plugin/session-start.sh`, `plugin/observe.sh`, and
`plugin/pre-tool.sh` as the hook script names. The actual agentmemory repository
uses `.mjs` ES modules in `plugin/scripts/` with distinct per-event names
(`session-start.mjs`, `prompt-submit.mjs`, `pre-tool-use.mjs`, `post-tool-use.mjs`,
etc.). This file reflects the verified reality; the brief's `.sh` names were design
placeholders.

The `hooks.json` in this directory covers all 9 events supported by marim's engine
(the brief's minimal 3-event example has been expanded to full coverage, matching
agentmemory's own `plugin/hooks/hooks.json`).
