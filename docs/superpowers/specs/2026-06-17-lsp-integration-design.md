# LSP integration — design

**Date:** 2026-06-17
**Status:** Approved, ready for implementation plan

## Goal

Give the harness semantic code intelligence backed by real language servers,
instead of relying solely on textual `grep`/`read_file`/`glob`. Two capabilities:

1. **Navigation tools** — go-to-definition, find-references, hover (type/signature/docs),
   document symbols, workspace symbols.
2. **Diagnostics feedback** — errors/warnings surfaced both automatically after edits
   and on demand, so the agent self-corrects.

Target languages: **Python, TypeScript/JavaScript, Java, C++** (the languages the
user works in), via a language-agnostic, config-driven client. Other languages
multilspy supports (Rust, Go, Ruby, C#, …) come along for free through the registry.

## Client layer decision: use `multilspy`

The hard part of an LSP client is not the JSON-RPC transport (a few hundred lines)
but the per-server quirks: Eclipse JDT LS needs a downloaded distribution, a
workspace data dir, and a long async init before it answers; clangd needs
`compile_commands.json`; typescript-language-server needs careful capability
negotiation and project-root detection. multilspy already encodes these.

`multilspy` (Microsoft) is purpose-built for *programmatic* LSP querying — not an
editor plugin — and its API maps 1:1 onto the tools we want:

```python
from multilspy import LanguageServer
from multilspy.multilspy_config import MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger

config = MultilspyConfig.from_dict({"code_language": "python"})  # one language per instance
lsp = LanguageServer.create(config, MultilspyLogger(), "/abs/project/root")
async with lsp.start_server():
    defs = await lsp.request_definition("src/module.py", line, col)   # 0-based
    refs = await lsp.request_references("src/module.py", line, col)
    hov  = await lsp.request_hover("src/module.py", line, col)
    syms = await lsp.request_document_symbols("src/module.py")
```

Supported `code_language` values include `python`, `typescript`, `javascript`,
`java`, `cpp`, `rust`, `csharp`, `go`, `ruby`, `dart`, `kotlin`, `php`, `elixir` —
covering the full target set. The async `LanguageServer` fits the harness's
asyncio/pydantic-ai model.

**External validation:** Serena (a mature LSP-for-coding-agents toolkit) is built
on a multilspy fork (`solidlsp`) and exposes essentially these tools — this exact
path has been validated in production.

Decision: start with upstream **`multilspy`** (standard, `uv add`-able). Switch to
`solidlsp` only if we hit a concrete language gap.

**Known caveat — diagnostics.** multilspy exposes no clean diagnostics request
method, because LSP diagnostics are *pushed* by the server
(`textDocument/publishDiagnostics`) asynchronously after a file opens/changes, not
request/response. Capturing them means opening/notifying the file and collecting
pushed notifications within a short settle window. This is isolated into its own
component and treated as best-effort.

## Architecture

New session-scoped subpackage `src/marim_harness/lsp/`, six read-only navigation
tools, and a best-effort diagnostics hook on edits. multilspy is the client layer;
the harness owns lifecycle, gating, coordinate translation, output formatting, and
diagnostics capture.

### `lsp/registry.py`

- Maps file extension → multilspy `code_language`:
  `.py`→python; `.ts/.tsx`→typescript, `.js/.jsx`→javascript; `.java`→java;
  `.cpp/.cc/.cxx/.h/.hpp/.hh`→cpp.
- Availability detection: report whether a language's server is usable (binary on
  PATH, or provided/auto-downloaded by multilspy), with an install hint per language.
- Optional config overrides: enable/disable a language; override its server command.
  Sourced from the harness's existing config mechanism.

### `lsp/manager.py` — `LspManager`

One instance per session, wired onto `Deps` by the Harness (like `jobs`/`hooks`).

- **Lazy start, per language:** starts one multilspy `LanguageServer` per language
  on first use, keyed by language, holding each open via an `AsyncExitStack` for the
  session. All servers shut down cleanly at teardown (server processes are children).
- **Routing:** workspace-relative path → its language (via registry) → its server.
- **Coordinate translation:** harness/`read_file` is **1-based** line/col; LSP is
  **0-based**. The manager translates on the way in and out so tools speak 1-based.
- **Output formatting:** compact, agent-readable text (`path:line:col` references are
  clickable; symbols as an outline). Bounded so large result sets don't flood context.
- **Timeout guard:** every call is timeout-bounded. A slow/missing/hung server
  degrades to a short message, never blocks the turn or raises.
- **Unavailable language:** returns `"No language server for <lang>; install <hint>"`
  rather than an exception.

### `lsp/diagnostics.py`

Best-effort diagnostics capture, isolated because it's the fiddly
async-notification part:

- Open / notify the server of the file (with current on-disk content, so it reflects
  a just-applied edit).
- Wait a short settle window; collect pushed `publishDiagnostics`.
- Return errors/warnings formatted compactly. On timeout, return
  `"no diagnostics (timed out)"`.

## Tools

Six tools, all **read-only** → registered in the read-tool set (not approval-gated,
like `read_file`/`grep`), and added to `_SUBAGENT_FNS` + the read-tool name set so
`explore`/`general` subagents can use them. Line/col are **1-based** to match what
the agent reads off `read_file`/`grep` output.

1. `goto_definition(path, line, col)` — where a symbol is defined.
2. `find_references(path, line, col)` — all uses of a symbol.
3. `hover(path, line, col)` — type/signature/docstring at a position.
4. `document_symbols(path)` — outline of one file.
5. `workspace_symbols(query)` — find a symbol by name across the project.
6. `diagnostics(path)` — on-demand errors/warnings for a file.

## Diagnostics-on-edit

- `Deps` gains `lsp: Optional[LspManager] = None`, wired by the Harness. `None` ⇒
  no-op everywhere (no servers configured/available → edits behave exactly as today).
- After `write_file`/`edit_file` succeeds, the tool calls `lsp.diagnostics(path)`
  best-effort with a short timeout and appends a compact summary to its result.
- Implemented **inside the provider tool functions** (`tools/provider.py`), not the
  external Claude-Code-compatible hook engine, since it needs the in-process client.

## Error handling

Every LSP path is best-effort and timeout-bounded: missing server, init failure,
hung request, or empty result all yield a short message — never a turn-failing
exception. Server subprocesses are torn down cleanly at session end.

## Dependency

`uv add multilspy`. First invocation per language may be slow (multilspy
auto-downloads some servers, e.g. Java JDT LS). clangd is already present on the dev
machine; pyright is already a CI dependency.

## Testing

Matches the existing test style (real subprocess servers are too heavy for the unit
suite):

- **Unit (no real server):** registry extension→language mapping, availability
  detection, 1-based↔0-based coordinate translation, output formatting, and the
  unavailable-language message.
- **Wiring:** mock `LspManager` to verify diagnostics-on-edit appends to
  `edit_file`/`write_file` results, and that `Deps.lsp=None` is a clean no-op.
- **Integration (optional):** one test against **pyright** (a dev dep we control),
  skipped if not installed.

## Scope boundaries (YAGNI)

Out of scope: rename / code-actions / formatting; completions as a tool (noise for
an agent); multi-root workspaces; LSP for languages beyond the registry. Navigation
+ diagnostics only.
