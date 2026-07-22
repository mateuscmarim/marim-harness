# LSP language servers as plugins

marim's language-server support is not a hard-coded list — it's assembled at
build time from **LSP providers**, one per language, contributed by plugins.
Four languages ship in-tree and always load; any other language server can be
added by installing a plugin with an `lsp` manifest block. marim never
downloads server binaries itself: a provider declares the binary it needs,
marim probes `PATH` for it, and on a miss surfaces the provider's install
hint.

Two independent env switches still gate the whole subsystem regardless of
which providers are registered: `MARIM_LSP` (the manager + diagnostics
appended to write/edit results) and `MARIM_LSP_TOOLS` (the six navigation
tools). See `.env.example`.

## Adding a language via a plugin

A plugin adds a language server by declaring an `lsp` block in its
`.marim-plugin/plugin.json` manifest — purely declarative, no in-tree code
required:

```json
{
  "name": "go-lsp",
  "lsp": {
    "language": "go",
    "extensions": [".go"],
    "command": "gopls",
    "args": [],
    "rootMarkers": ["go.mod", "go.work"],
    "env": { "GOFLAGS": "-mod=mod" },
    "probe": ["gopls"],
    "installHint": "install gopls (go install golang.org/x/tools/gopls@latest)"
  }
}
```

- `language` — canonical id used for tool-facing language names.
- `extensions` — file extensions routed to this provider.
- `command` / `args` — the server launched over generic stdio LSP
  (`GenericStdioServer`, reusing multilspy's client plumbing: initialize
  handshake, definition requests, the diagnostics-publish path). The existing
  `${MARIM_PLUGIN_ROOT}` substitution applies, so a plugin can ship a wrapper
  script alongside its manifest.
- `rootMarkers` / `env` — optional workspace-root detection files and extra
  environment variables for the launched process.
- `probe` — binaries checked on `PATH` before the server is considered
  available; defaults to `[command]` when omitted, and an empty list means
  "always available" (nothing to probe).
- `installHint` — surfaced verbatim when the probe misses.

`lsp` may also be a list, for a plugin that contributes more than one
language.

`backend` and a named `diagnostics` value are a **bundled-only** escape hatch
into in-tree tuned code (`BasedPyrightServer`, multilspy's per-language tuned
servers, `lsp/checks.py`'s ruff/pyright pipeline) — a private implementation
seam, not a public manifest API. A third-party (non-bundled) manifest using
either key is rejected by strict parsing (`load_manifest`) and silently
dropped by lenient parsing (`try_load_manifest`); a third-party plugin cannot
select `backend: basedpyright`. Declarative `command`/`args` is the only path
open to third-party plugins.

## The four bundled languages

These ship in-tree at `src/marim_harness/lsp/bundled/{python,typescript,cpp,java}/.marim-plugin/plugin.json`
and always load (bundled plugins are always trusted):

| Language   | Backend                 | Diagnostics     | Extensions                                     | Probe                                                 | Install hint |
|------------|--------------------------|------------------|-------------------------------------------------|--------------------------------------------------------|--------------|
| python     | `basedpyright` (jedi fallback) | `python-checks` (ruff always + pyright on deep, via `lsp/checks.py`) | `.py`                                           | `basedpyright-langserver`, `jedi-language-server` | `pip install basedpyright` / `pip install jedi-language-server` |
| typescript | `multilspy:typescript`  | `lsp` (server's own `publishDiagnostics`) | `.ts` `.tsx` `.js` `.jsx` `.mjs` `.cjs`         | `typescript-language-server`                            | `npm i -g typescript-language-server typescript` |
| cpp        | `multilspy:cpp`         | `lsp`            | `.cpp` `.cc` `.cxx` `.c` `.h` `.hpp` `.hh`      | `clangd`                                                | `pacman -S clang` (or your platform's clang/clangd package) |
| java       | `multilspy:java`        | `lsp`            | `.java`                                         | *(none — always considered available)*                  | auto-downloaded by multilspy on first use |

The java row is the one accuracy nuance: marim itself never downloads server
binaries, but *multilspy* (the underlying library the bundled `java` provider
delegates to) auto-downloads eclipse jdt.ls on first use. That auto-download
is multilspy's behavior behind a bundled plugin, not something marim does for
plugin-declared servers in general — a third-party `command`/`args` provider
gets no such treatment; its binary must already be on `PATH`.

## Trust

Launching a plugin-declared binary (or the process behind a declarative
`command`) is arbitrary code execution on connect — the same risk class as an
MCP server, and third-party LSP providers follow the **exact same rule**:

- Bundled providers are always trusted.
- Global/user-installed plugins load their LSP providers once the plugin
  itself is trusted (the per-plugin `trusted` bit).
- Project-scope plugins require **both** the per-plugin `trusted` bit **and**
  the project itself being trusted (`MARIM_TRUST_PROJECT_HOOKS=1`) — the same
  gate as `.marim/hooks.json` and `.marim/mcp.json`.

An untrusted provider contributes nothing: its extensions never route to it,
its binary is never launched, and it never registers navigation tools for its
language.

## Assembly and precedence

The registry assembled at build time is `bundled providers + trusted
third-party providers`, with bundled providers at **lowest** precedence — a
plugin that declares a provider for an extension a bundled language already
owns (e.g. a plugin adding its own `.py` handling) overrides the bundled one
for that extension.
