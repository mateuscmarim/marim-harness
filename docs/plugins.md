# Plugins

A plugin bundles skills, sub-agents, hooks, MCP servers, language servers, and
optional `AGENTS.md` instructions into one installable directory.

## Layout

    my-plugin/
    ├── .marim-plugin/plugin.json   # manifest (name required; optional "lsp" block)
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

## Language servers (`lsp`)

A plugin can add a language server by declaring an `lsp` block in its
`plugin.json` manifest (object or list, for multiple languages) — purely
declarative: `language`, `extensions`, `command`, `args`, `env`, `probe`,
`installHint`, `rootMarkers` (reserved — parsed but not yet honored; servers
currently launch at the workspace root). The declared command is launched
over generic stdio LSP; there's no separate file, unlike `mcp.json`. `backend`
and a named `diagnostics` value are a bundled-only seam into marim's in-tree
tuned servers (used by the four language plugins marim ships in-tree) — a
third-party manifest using either key is rejected (strict parse) or dropped
(lenient parse). See `docs/lsp-plugins.md` for the full manifest reference,
the four bundled languages, and the declare/probe/install-hint model.

## Trust

Skills, sub-agents, and instructions load for any enabled plugin — except that
project-scope plugins also require the project itself to be trusted (see
below). Hooks, MCP servers, and LSP providers execute code, so they load only
for plugins you trust — an untrusted plugin's `lsp` block contributes no
provider (its extensions never route to it, and its server binary is never
launched). Installing a plugin with hooks/MCP/`lsp` prompts for trust; pass
`--trust` to grant it non-interactively (e.g. in CI). Trust is recorded per
plugin.

*Project-scope* plugins additionally require the project itself to be trusted
(`MARIM_TRUST_PROJECT_HOOKS=1`, the same gate as `.marim/hooks.json` and
`.marim/mcp.json`) before contributing anything at all. Their registry —
enabled/trusted bits included — is committed to the repo, so on a freshly
cloned repo those bits are whoever-committed-it's word, not yours. An
untrusted project's plugins contribute nothing: skills, sub-agents, and
instructions are withheld by the project-trust gate alone (no per-plugin trust
needed, since inert text doesn't execute code), while hooks/MCP/`lsp`
additionally require the per-plugin trust bit once the project is trusted.
Bundled LSP providers (the four languages marim ships in-tree) are exempt —
they're always trusted, the same as bundled skills/agents.

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
