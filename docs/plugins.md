# Plugins

A plugin bundles skills, sub-agents, hooks, MCP servers, and optional
`AGENTS.md` instructions into one installable directory.

## Layout

    my-plugin/
    ├── .marim-plugin/plugin.json   # manifest (name required)
    ├── skills/<name>/SKILL.md
    ├── agents/<name>.md
    ├── hooks/hooks.json
    ├── mcp.json
    └── AGENTS.md

## Install

    marim plugin install <path|git-url> [--scope global|project] [--trust] [--link]
    marim plugin list
    marim plugin enable|disable|trust|remove|update <name>
    marim plugin validate <path>

In the TUI: `/plugin [list | enable <name> | disable <name>]`.

## Trust

Skills, sub-agents, and instructions load for any enabled plugin. Hooks and MCP
servers execute code, so they load only for plugins you trust. Installing a
plugin with hooks/MCP prompts for trust; pass `--trust` to grant it
non-interactively (e.g. in CI). Trust is recorded per plugin.

*Project-scope* plugins additionally require the project itself to be trusted
(`MARIM_TRUST_PROJECT_HOOKS=1`, the same gate as `.marim/hooks.json` and
`.marim/mcp.json`) before their hooks/MCP load. Their registry — trust bit
included — is committed to the repo, so on a freshly cloned repo that bit is
whoever-committed-it's word, not yours. Skills, sub-agents, and instructions
from project plugins are unaffected.

> **Note:** Toggling a plugin-provided MCP server via the MCP UI (e.g. `/mcp
> disable <name>`) is session-only and not persisted; use `marim plugin disable
> <name>` (or `/plugin disable <name>`) to persist the change across launches.

## Naming

Plugin skills and sub-agents are namespaced `plugin-name:item-name`, so they
never collide with your own. Your own skills/agents always take precedence.

## Scopes

`--scope global` (default) installs to `~/.config/marim/plugins/`; `--scope
project` installs to `<workspace>/.marim/plugins/` for sharing via git.
Enable/disable and trust changes to hooks/MCP take effect on next launch.
