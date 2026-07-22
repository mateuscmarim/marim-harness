# LSP language servers as installable plugins

**Date:** 2026-07-21
**Status:** Design approved, pending implementation plan

## Problem

marim's LSP support is a *closed set*. `lsp/registry.py` hard-codes two module-level
maps — `_EXT_TO_LANG` (file extension → language) and `_PROBES` (language → probe
binaries + install hint) — and `lsp/manager.py`'s `_default_factory` launches every
server through **multilspy**, whose per-language server implementations are themselves
a fixed repertoire. Adding a language means editing marim's source.

Claude Code, by contrast, ships language servers as **installable plugins** from a
marketplace: a plugin declares which server to run and how, marim probes for the
binary, and the user installs the binary themselves. This spec brings that model to
marim, reusing the existing plugin subsystem (`plugins/`) rather than inventing a new
one.

## Decisions (from brainstorming)

1. **Scope: any LSP server via a generic launcher.** A plugin can bring *any* language
   server — not just languages multilspy already knows — by declaring a command to run
   over generic stdio LSP. This is the Claude-Code-shaped answer and the biggest single
   piece of new work.
2. **Migrate everything to bundled plugins.** Even the current built-ins
   (python→basedpyright, typescript/javascript, cpp, java) become bundled "official"
   plugins shipped in-tree. There is no hard-coded language set after this change; the
   registry is assembled entirely from discovered providers. This keeps one uniform
   code path.
3. **Declare + probe + hint (like CC).** A plugin declares the binary it needs; marim
   probes `PATH` and, on a miss, surfaces the install hint. **marim never downloads
   binaries.** (multilspy's existing java auto-download survives only as built-in-server
   behavior behind a bundled plugin.)
4. **Reuse the MCP/hooks trust gate.** Launching a plugin-declared binary is arbitrary
   code execution on connect — the same risk class as an MCP server. Bundled and
   global/user-installed plugins always load their LSP servers; **project-local** plugins
   launch their binary only under `MARIM_TRUST_PROJECT_HOOKS`, identical to project-local
   MCP servers and hooks.

## Architecture

### Provider model

The pure-module `registry.py` (global `_EXT_TO_LANG` / `_PROBES` dicts) is replaced by
an **`LspRegistry` instance** assembled at build time from a set of **LSP providers**.
A provider is one language's contribution:

```
provider = {
  language:     canonical id (e.g. "go"),
  extensions:   [".go"],
  probe:        binaries checked on PATH (empty ⇒ always available),
  installHint:  surfaced verbatim on a probe miss,
  launch:       generic stdio (command/args/rootMarkers/env)  OR  named in-tree backend,
  diagnostics:  "lsp" (publishDiagnostics)                     OR  named in-tree checker,
}
```

Everything the module exposes today — `language_for`, `availability`,
`workspace_languages`, `locally_installed_languages` — becomes a method on the
`LspRegistry` instance, operating over the injected provider set. No global mutable
state; the registry is per-session, matching how plugins already work.

### Provider sources

Both sources flow through the **existing plugin discovery**:

1. **Bundled official plugins** — shipped in-tree (e.g.
   `lsp/bundled/{python,typescript,cpp,java}/.marim-plugin/plugin.json`), discovered via
   the same bundled-plugin mechanism plugins already use. Always trusted. These may use
   the **named-backend escape hatch** (below) that maps to in-tree tuned code.
2. **Third-party plugins** — declare an `lsp` block in their manifest, purely
   declarative (`command`/`args`). Subject to the trust gate.

### Data flow

```
plugin discovery
  → collect eligible `lsp` blocks (tagged with source: bundled / global / project-local)
  → trust filter (drop untrusted project-local providers)
  → build LspRegistry (bundled providers + trusted third-party providers)
  → injected into BOTH:
       • bootstrap's tool-registration gate (workspace_languages + availability)
       • LspManager (per-call path→language, availability, launch spec)
```

## Manifest schema

The `lsp` key in `.marim-plugin/plugin.json` is a provider object or a list of them.

### Third-party (declarative) form

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

- `command`/`args` — the generic stdio launcher. The existing `${MARIM_PLUGIN_ROOT}`
  substitution applies (a plugin can ship a wrapper script).
- `probe` defaults to `[command]` when omitted; an empty list means "always available"
  (auto-provided).
- `installHint` is surfaced verbatim when the probe misses.

### Bundled-only escape hatch

A `backend` key naming an in-tree provider, **mutually exclusive with `command`**:

```json
{ "lsp": { "language": "python", "extensions": [".py"],
           "backend": "basedpyright", "diagnostics": "python-checks",
           "probe": ["basedpyright-langserver", "jedi-language-server"],
           "installHint": "install basedpyright (pip install basedpyright) or jedi-language-server (pip install jedi-language-server)" } }
```

- Recognized `backend` values map to in-tree factories: `basedpyright` → the
  `BasedPyrightServer` subclass; `multilspy:<lang>` → multilspy's tuned server for that
  language.
- `diagnostics` is `"lsp"` (default, publishDiagnostics path) or a named checker such as
  `"python-checks"` → `lsp/checks.py`.
- **A non-bundled plugin using `backend` or a named `diagnostics` is rejected at parse**
  (strict `load_manifest`) / ignored (lenient `try_load_manifest`). Third-party plugins
  get the declarative path only — the named-backend seam stays a private in-tree
  implementation detail, not a public API.

Manifest parsing follows the existing strict/lenient split in `manifest.py`, with the
same path-traversal guards for any script paths.

## Manager & generic launcher changes

- `LspManager.__init__` gains a `registry: LspRegistry` param (injected — no module
  import). `_server_for`, `diagnostics`, and `workspace_symbols` call `self._registry.*`
  instead of the module.
- `_default_factory` becomes **registry-driven**: it asks the provider for its launch
  spec.
  - `backend: basedpyright` → `BasedPyrightServer`
  - `backend: multilspy:<lang>` → `LanguageServer.create`
  - declarative `command`/`args` → a new **`GenericStdioServer(multilspy.LanguageServer)`**
    built from a `ProcessLaunchInfo`, reusing all of multilspy's LSP client machinery
    (initialize handshake, `request_definition`, the `DiagnosticsCollector` attach, the
    publish-signal wrapper).
- `diagnostics()` stops hard-coding `language_for(path) == "python"`. It reads the
  provider's **diagnostics strategy**: `"python-checks"` → `checks.python_diagnostics`;
  `"lsp"` → the existing publishDiagnostics path. Behavior for python is unchanged, now
  generalized so a future bundled plugin can add an external-checker language.

`lsp/generic.py` (the `GenericStdioServer` subclass) is the one genuinely new
implementation file: a generic subclass with sensible default `initialize` params plus
configurable root markers.

## Migrating the built-ins to bundled plugins

Four bundled plugins ship in-tree, each carrying the exact extensions, probe binaries,
and install hints currently in `_PROBES`/`_EXT_TO_LANG`, so behavior is preserved
byte-for-byte:

| Plugin       | language(s)            | backend                | diagnostics     | probe                                                |
|--------------|-----------------------|------------------------|-----------------|------------------------------------------------------|
| `python`     | python                | `basedpyright`         | `python-checks` | `basedpyright-langserver`, `jedi-language-server`    |
| `typescript` | typescript, javascript | `multilspy:typescript` | `lsp`           | `typescript-language-server`                         |
| `cpp`        | cpp                   | `multilspy:cpp`        | `lsp`           | `clangd`                                             |
| `java`       | java                  | `multilspy:java`       | `lsp`           | *(empty → auto-provided by multilspy)*               |

After migration, `registry.py` keeps only the pure helpers (the `language_for` split
logic, the scan/coverage thresholds `_MIN_SHARE`/`_MIN_COUNT`, `Availability`, the
`_SCAN_IGNORED_DIRS` walk) — now operating over the injected provider set instead of
module globals. `basedpyright.py` and `checks.py` are unchanged; they are simply reached
via the `backend`/`diagnostics` named keys rather than the `if language == "python"`
branches.

## Trust gating & build-time integration

- **Discovery** collects `lsp` providers alongside the skills/agents/hooks/mcp it already
  gathers per plugin, tagging each provider with its source (bundled / global /
  project-local) so the trust decision is data, not a re-scan.
- **Trust filter** reuses the exact predicate that already gates project-local MCP/hooks
  (`MARIM_TRUST_PROJECT_HOOKS`). Untrusted project-local LSP providers are dropped from
  the registry — their binary is never launched and their extensions never register
  tools.
- **`bootstrap.py` (lines ~104-116)** builds the `LspRegistry` from the trusted provider
  set, then runs the *same* `workspace_languages` + `availability` gate against it to
  decide whether to register the six LSP tools. `builder.py`'s `.with_lsp(...)` grows a
  param to carry the registry (or provider list) through to `LspManager`. `HarnessBuilder`
  stays env-free — bootstrap does the discovery/trust reading, matching the existing
  split.

## Testing

- **Pure/unit:** manifest `lsp`-block parsing (valid, list form, `backend` bundled-only
  rejection, path traversal); `LspRegistry` merge + ext→language + availability + trust
  filtering — all side-effect-free, tested directly per the repo convention.
- **Generic launcher:** `GenericStdioServer` against a stub/fake LSP server (no real
  binary) — initialize handshake, a definition round-trip, the diagnostics publish path.
  Tests pin behavior against the stub so we don't couple to multilspy version quirks.
- **Migration guard:** a test asserting the four bundled plugins reproduce today's
  `_EXT_TO_LANG`/`_PROBES` mapping exactly (regression fence).
- **Integration:** the bootstrap gate registers/skips tools correctly given a workspace +
  provider set; python still routes to `checks.py`.
- Full gate before done: `ruff → pyright → pytest` on the CI matrix (Python 3.10, 3.12,
  3.14), per CLAUDE.md.

## Out of scope (YAGNI)

- Downloading/installing language-server binaries (decision 3).
- A plugin marketplace UI / remote fetch — plugins install through the existing plugin
  install flow.
- Per-language LSP configuration passthrough beyond `env` and `rootMarkers` (can be added
  later behind the same manifest block if a real need appears).
- `find_implementations` / call-hierarchy tools (CC has these; marim's six-tool surface
  is unchanged by this work).
