# Plugin System Design

**Date:** 2026-06-22
**Status:** Approved — ready for implementation planning
**Scope:** Milestone A (plugin format + local/git install). Milestone B (marketplace) is a designed-for fast-follow.

## Summary

Add a Claude-Code-style plugin system to marim. A **plugin** is a directory with a
manifest that bundles marim's existing extension primitives — skills, subagents,
hooks, MCP servers, and optional instructions — into one installable, versioned,
shareable unit.

The key insight from exploring the codebase: marim needs **no new capability
machinery**. Every primitive a plugin bundles already has a discovery system, and
they all share one shape — a `roots()` function returning `(source, Path)` tuples
iterated in precedence order and deduped by name (skills, subagents), or a config
merge over global+project files (hooks, MCP). A plugin is therefore mechanically
*"a folder that contributes extra discovery roots and extra config specs to systems
that already exist,"* plus install plumbing and a trust gate.

Field names and on-disk conventions deliberately mirror Claude Code
(`.marim-plugin/plugin.json`, `skills/`, `agents/`, `hooks/hooks.json`, `mcp.json`,
`${MARIM_PLUGIN_ROOT}`) so the mental model transfers for anyone who has used CC
plugins.

## Goals

- A plugin format that bundles skills, subagents, hooks, MCP servers, and optional
  `AGENTS.md` instructions.
- Install from a **local directory** or a **git URL**.
- **Global and project** install scopes, consistent with marim's existing
  global (`~/.config/marim/`) vs project (`<workspace>/.marim/`) split.
- A **trust-on-install** model: inert content (skills/agents/instructions) always
  loads; executable content (hooks/MCP) loads only for plugins the user has trusted.
- A `marim plugin …` CLI and a minimal `/plugin` TUI command.
- On-disk layout and manifest schema designed so the marketplace (Milestone B) is
  purely additive — no redesign.

## Non-Goals (Milestone A)

- Marketplace registry, `marketplace.json`, `plugin@marketplace` syntax, interactive
  4-tab browser. (Milestone B.)
- Runtime hot-reload of plugins. Enable/disable/install changes take effect on the
  next launch (marim builds the harness once at startup).
- A blanket trust escape-hatch env var. Headless trust is per-install via `--trust`.
- New executable primitives beyond what hooks/MCP already provide (no in-process
  Python extension loading).

## What a Plugin Is

### Directory layout

```
my-plugin/
├── .marim-plugin/
│   └── plugin.json          # manifest — the ONLY file in .marim-plugin/
├── skills/
│   └── <name>/SKILL.md       # bundled skills    → feeds discover_skills
├── agents/
│   └── <name>.md             # bundled subagents → feeds discover_agents
├── hooks/
│   └── hooks.json            # bundled hooks     → feeds load_hooks_config
├── mcp.json                  # bundled MCP servers → feeds load_mcp_config
├── AGENTS.md                 # optional bundled instructions
└── README.md
```

All component directories live at the plugin root. Only `plugin.json` lives inside
`.marim-plugin/`.

### Manifest schema (`.marim-plugin/plugin.json`)

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "What this plugin does",
  "author": {"name": "Author Name", "email": "author@example.com"},
  "homepage": "https://example.com",
  "repository": "https://github.com/author/my-plugin",
  "license": "MIT",
  "keywords": ["example"],
  "skills": "./skills/",
  "agents": "./agents/",
  "hooks": "./hooks/hooks.json",
  "mcpServers": "./mcp.json"
}
```

- **Required:** `name` (kebab-case identifier).
- **Component paths** (`skills`, `agents`, `hooks`, `mcpServers`) are optional and
  default to the conventional locations above. They may be a relative path string;
  `mcpServers`/`hooks` may alternatively be an inline object (parity with CC). Paths
  must be relative and resolve against the plugin root. No `../` traversal.
- **Metadata** (`version`, `description`, `author`, `homepage`, `repository`,
  `license`, `keywords`) is informational and surfaced by `marim plugin info`/`list`.
- Unrecognized fields are ignored (forward-compat / multi-ecosystem manifests);
  `marim plugin validate` warns about likely misspellings.

### Path variable

Inside bundled hook commands and MCP server specs, `${MARIM_PLUGIN_ROOT}` substitutes
to the absolute path of the plugin's installed directory, so bundled scripts can be
referenced portably (e.g. `"command": "${MARIM_PLUGIN_ROOT}/bin/lint.sh"`).

## Disk Layout, State & Scopes

Install **copies** the plugin into a per-scope cache directory (so the origin can move
or disappear without breaking the install, and installs are reproducible). The origin
is recorded for `update`.

```
~/.config/marim/plugins/          # global scope
├── plugins.json                  # state registry for this scope
└── <plugin-name>/                # copied plugin contents
    └── .marim-plugin/plugin.json …

<workspace>/.marim/plugins/       # project scope — identical shape
├── plugins.json
└── <plugin-name>/
```

### State registry (`plugins.json`)

One file per scope. Maps plugin name → record:

```json
{
  "plugins": {
    "my-plugin": {
      "version": "1.0.0",
      "source": {"type": "local", "path": "/abs/path/to/src"},
      "enabled": true,
      "trusted": false,
      "linked": false,
      "installed_at": "2026-06-22T12:00:00Z"
    },
    "other-plugin": {
      "version": "2.1.0",
      "source": {"type": "git", "url": "https://github.com/x/y.git",
                 "ref": "v2.1.0", "sha": "abc123…"},
      "enabled": true,
      "trusted": true,
      "linked": false,
      "installed_at": "2026-06-22T12:05:00Z"
    }
  }
}
```

- `source.type` is `"local"` or `"git"`. `linked: true` means the cache entry is a
  symlink to a live local source (`--link`, for plugin authors) rather than a copy.
- Timestamps stored as ISO-8601 UTC.

### Precedence

Plugin-provided skills and subagents are **namespaced** (`plugin-name:item-name`, see
[Namespacing](#namespacing)), so they occupy a distinct key from your bare-named local
items and cannot shadow them by construction — your own files always win. Precedence
ordering therefore governs two things: stable, predictable layering across tiers, and
the one collision that *can* occur — the **same plugin name installed in both scopes**,
where the project copy wins over the global copy.

Discovery order, highest precedence first:

1. Project user dirs — `<ws>/.marim/skills`, `<ws>/.marim/agents` (bare names)
2. Global user dirs — `~/.config/marim/skills`, `~/.config/marim/agents` (bare names)
3. **Project plugins** — enabled plugins in `<ws>/.marim/plugins/` (`plugin:name`)
4. **Global plugins** — enabled plugins in `~/.config/marim/plugins/` (`plugin:name`)

This matches Claude Code's "user content beats plugins" guarantee — here enforced by
namespacing rather than by name-shadowing. Built-in subagents (`explore`, `general`)
remain the final fallback as today.

## Trust Model

Plugins can bundle **hooks** and **MCP servers**, both of which execute arbitrary code.
marim already guards project hooks behind `MARIM_TRUST_PROJECT_HOOKS`; installing a
plugin from a git URL is exactly the "running a stranger's code" moment that warrants
an explicit decision.

- **On install**, marim parses the manifest and reports the bundle contents
  (`N skills, N agents, N hooks, N MCP servers`). If the plugin contains **hooks or
  MCP servers**, it prompts the user to trust the plugin once. The decision is stored
  as `trusted` in `plugins.json`. A plugin with no executable parts is recorded
  `trusted: true` automatically (nothing to gate).
- **Loader gating:**
  - Skills, subagents, and instructions **always load** for enabled plugins — inert
    text the model reads.
  - Hooks and MCP specs **merge only when `enabled && trusted`**. An untrusted plugin
    still contributes its skills/agents; its executable parts stay dormant until the
    user runs `marim plugin trust <name>`.
- **Headless / CI:** install defaults to `trusted: false`. `--trust` is required to
  mark a plugin trusted non-interactively. There is no blanket
  "trust all plugins" env var.
- This per-plugin trust is **independent** of `MARIM_TRUST_PROJECT_HOOKS`, which
  continues to govern the loose `.marim/hooks.json` file.

## Integration Seams

A new `src/marim_harness/plugins/` package:

- `manifest.py` — parse & validate `plugin.json`; resolve component paths against the
  plugin root; substitute `${MARIM_PLUGIN_ROOT}`.
- `state.py` — read/write `plugins.json` per scope; the `InstalledPlugin` record.
- `discovery.py` — given a workspace, enumerate enabled plugins across both scopes and
  expose their contributed roots and (trust-gated) hook/MCP specs.
- `install.py` — fetch (local copy / git clone), validate, copy into cache, write
  state, run the trust prompt.

Thin edits at the existing seams (all confirmed present, same shape):

| Seam | File | Change |
|------|------|--------|
| Skill roots | `workspace/skills.py` `skill_roots()` | Append `(f"plugin:{name}", root/"skills")` per enabled plugin, after user roots |
| Agent roots | `workspace/agents.py` `agent_roots()` | Append `(f"plugin:{name}", root/"agents")` per enabled plugin, after user roots |
| MCP config | `mcp/config.py` `load_mcp_config()` | Merge specs from enabled+trusted plugins (with `${MARIM_PLUGIN_ROOT}` substituted) |
| Hooks config | `hooks/config.py` `load_hooks_config()` | Merge entries from enabled+trusted plugins |
| Bootstrap | `bootstrap.py` `build_harness()` | Load the plugin set once and thread it into the above |
| Instructions | `instructions.py` | Optionally inject each enabled plugin's bundled `AGENTS.md` as an instruction closure |

The roots functions currently take `workspace_root`; they will additionally consult the
discovered plugin set. To keep them pure and testable, the plugin set is resolved once in
`build_harness()` and passed down (rather than each roots function re-scanning disk).

### Namespacing

Plugin-provided skills and subagents are exposed as `plugin-name:item-name`, mirroring
Claude Code. This guarantees two plugins — or a plugin and a user's own files — never
collide on a bare name. The `activate_skill` and `spawn_agent` flows accept the
namespaced identifier. The loader sets the `Skill.name` / `AgentDef.name` to the
namespaced form and records the originating plugin in `source` (e.g. `plugin:my-plugin`).

### Error handling

Consistent with marim's existing fail-safe convention (a broken skill/hook file must
never break a turn):

- **Discovery is lenient:** a malformed manifest or component is skipped with a logged
  warning; the rest of the plugin and other plugins still load.
- **Install is strict:** `marim plugin install` and `marim plugin validate` reject a
  malformed manifest with a clear error rather than installing a broken plugin.

## CLI Surface (Milestone A)

Added via marim's existing hand-rolled router: `"plugin"` joins the `_MANAGEMENT` set in
`interfaces/cli/__init__.py`, and a new `interfaces/cli/plugin.py` exposes
`main(argv) -> int` building its own argparse parser (same pattern as `config.py` /
`sessions.py`).

```
marim plugin install <path|git-url> [--scope global|project] [--trust] [--link] [--name X]
marim plugin list [--json]
marim plugin info <name>
marim plugin enable  <name> [--scope global|project]
marim plugin disable <name> [--scope global|project]
marim plugin trust   <name> [--scope global|project]
marim plugin remove  <name> [--scope global|project]
marim plugin update  <name> [--scope global|project]   # re-fetch git source
marim plugin validate <path>                           # lint a plugin you are authoring
```

- `--scope` defaults to `global`.
- `--link` (local sources only) symlinks the cache entry to the live source for plugin
  development instead of copying.
- `--name` overrides the installed name (collision resolution / local testing).
- `install` from a git URL: shallow-clone to a temp dir, validate the manifest, copy
  into the cache, record `source: {type: git, url, ref, sha}`. `update` re-clones.

## TUI Surface (Milestone A)

A `/plugin` slash command registered in `interfaces/tui/commands.py` (append a
`Command("plugin", …, _cmd_plugin)` to `COMMANDS`), reusing the same core as the CLI:

- `/plugin` or `/plugin list` — list installed plugins with scope, enabled, trusted.
- `/plugin enable <name>` / `/plugin disable <name>`.

Because the harness is built once at startup, enable/disable persists to `plugins.json`
and takes effect on next launch; the command states this in its confirmation message.
The richer interactive browser ships with the marketplace in Milestone B.

## Testing

Fixtures under `tests/fixtures/plugins/` (a valid plugin with all four component types;
an inert-only plugin; a malformed-manifest plugin).

**Unit:**
- Manifest parse & validate: required fields, path resolution, `${MARIM_PLUGIN_ROOT}`
  substitution, rejection of `../` traversal, lenient vs strict behavior.
- State registry: read/write round-trip, per-scope isolation, missing-file → empty.
- Root injection & precedence: user files beat plugins; project plugins beat global
  plugins; built-in agents remain the final fallback.
- Trust gating: inert content loads regardless of trust; hooks/MCP merge only when
  enabled+trusted.
- Namespacing & collision: two plugins providing same bare skill name coexist as
  distinct `plugin:name` entries.
- Git source recording (mock the clone): `source` fields populated; `update` re-fetches.

**Integration:**
- Install a fixture plugin from a local dir → assert its skills and subagents surface
  (namespaced) and, when trusted, its hooks/MCP register.
- Assert an untrusted plugin leaves hooks/MCP dormant while still exposing skills/agents.
- enable / disable / trust / remove round-trip through `plugins.json` and reflected in a
  freshly built harness.

## Milestones

- **Milestone A (this spec):** plugin format, manifest, local + git install, trust model,
  global + project scopes, `marim plugin …` CLI, minimal `/plugin` TUI command, tests.
  On-disk layout and manifest designed marketplace-ready.
- **Milestone B (fast-follow, additive):** `.marim-plugin/marketplace.json`,
  `marim plugin marketplace add <git-url>`, `plugin@marketplace` install syntax, and the
  interactive `/plugin` browser. No redesign of A required.
