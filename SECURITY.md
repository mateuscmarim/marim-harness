# Security Policy

## Reporting a vulnerability

Please report security issues **privately** to **mateus@marim.dev** — do not
open a public issue. Include a description, reproduction steps, and the impact
you believe it has. You'll get an acknowledgement within a few days, and a fix
or a coordinated disclosure plan as soon as one exists.

Only the latest release (and `master`) receive security fixes.

## Trust model

marim is a coding agent: it runs shell commands, edits files, and can load
project-local configuration. Knowing where the trust boundaries sit matters
more here than for most tools.

**What the agent may do is gated at three layers:**

1. **Permission modes.** `auto` runs tools freely, `ask` requires interactive
   approval for every gated tool (`bash`, `write_file`, `edit_file`, …), and
   `plan` is read-only. `MARIM_DEFAULT_MODE` set in a *project* `.env` is
   deliberately ignored — a cloned repo must not self-elevate.
2. **Command policy.** `MARIM_COMMAND_DENYLIST` / `MARIM_COMMAND_ALLOWLIST`
   gate the shell tool in `auto` and `ask` modes; deny takes precedence.
3. **Path guards.** File tools are confined to the workspace (plus the
   per-session scratchpad); writes outside it are rejected.

**Project-local executable config is opt-in.** Hooks (`.marim/hooks.json`),
MCP servers (`.marim/mcp.json`), project-scope plugins, and third-party LSP
manifests all launch code from the repository on startup. They load **only**
when `MARIM_TRUST_PROJECT_HOOKS=1` is set. Treat that variable as a
supply-chain decision: leave it unset for repositories you don't fully
control. Global (user-level) hooks, MCP servers, and the four bundled LSP
plugins always load — they come from your machine, not the repo.

**Credentials.** API keys are read only from the environment / `.env` files.
They are never written to session files, logs, or provider-error dumps.

**Language servers.** marim never downloads LSP server binaries — it only
probes `PATH` for ones you've installed and surfaces an install hint
otherwise.

If you find a way for a *repository's contents* to cause code execution
without `MARIM_TRUST_PROJECT_HOOKS` (or for a gated tool to run without
approval in `ask`/`plan` mode), that is exactly the kind of report we want.
