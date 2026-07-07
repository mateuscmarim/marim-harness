# Forge integration via `tea` — a Pydantic AI toolset with a pluggable backend

**Date:** 2026-07-07
**Status:** Design approved, ready for implementation plan
**Scope:** Agent-facing tools for pull requests + CI/actions, delivered as a Pydantic AI `FunctionToolset` that calls through a forge-backend seam. v1 ships one backend — `tea` (Gitea). The seam is built so a future `gh` (GitHub) backend is one new file plus one selection branch. A read-only TUI surface is explicitly **out of scope** (follow-up spec).

## Goal

Give marim a first-class, agent-facing capability to work with the project's forge (`git.marim.dev`, Gitea) the way Claude Code leans on `gh`: open a PR for the current branch, inspect PRs, and answer "what's failing in CI?" — without dropping to raw `bash`. The capability is a set of **dedicated, typed, forge-agnostic tools** grouped into one Pydantic AI toolset. The concrete CLI (`tea` today, `gh` later) sits behind a backend interface.

### Non-goals (v1)

- The `gh`/GitHub backend itself — the seam is built and documented, but only `TeaBackend` is implemented.
- Issues, comments/reviews, labels, milestones, releases — deferred.
- Any TUI surface (CI status indicator, PR panel) — separate follow-up spec.
- Granting the forge tools to sub-agents — main-loop only in v1.
- Managing forge auth/config — `tea` is already logged in (`git-marim-dev`, default login); the backend relies on the CLI's own config.

## Context

- `tea` v0.14.2 is installed at `/usr/bin/tea`; the repo remote is `https://git.marim.dev/mateuscmarim/marim-harness.git`.
- Every relevant tea subcommand supports `--output json`, and PR listing supports `--fields` selection (including `mergeable` and a `ci` status field), so structured parsing is robust rather than table-scraping. `gh` offers equivalent `--json` output, so the neutral models below map cleanly to both.
- marim registers builtin tools via `provider.register(agent)` at construction and passes MCP tools as `toolsets` into `agent.run` per turn. Pydantic AI (1.107.0) `FunctionToolset.add_function`/`.tool` accept `requires_approval`, so one toolset can hold ungated reads and gated writes.
- marim gates approval **per tool**: read tools are ungated, mutating/outbound tools use `requires_approval=True` and defer through `resolve_approvals` (`auto` runs, `ask` prompts, `plan` denies).

## Architecture

A self-contained subsystem following marim's pure-core / thin-tool split (mirrors `lsp/`, `mcp/`), organized around a **forge-backend seam** so the concrete CLI is swappable:

```
src/marim_harness/forge/
  __init__.py
  models.py        # forge-NEUTRAL frozen dataclasses: PullRequest, CiRun, CiStatus; ForgeError
  backend.py       # ForgeBackend protocol (the seam) — 5 methods returning neutral models
  tea_backend.py   # TeaBackend implements ForgeBackend: the _run_tea choke point,
                   #  pure argv-builders and tea-JSON→neutral-model mappers,
                   #  tea_available() (PATH + default login)
  select.py        # select_backend(cfg) -> ForgeBackend | None  (v1: tea only; gh is a future branch)
src/marim_harness/tools/forge_tools.py
                   # the 5 forge-agnostic tool functions + build_forge_toolset(backend) -> FunctionToolset
```

- **`models.py`** — the neutral vocabulary shared by all backends. Nothing tea- or gh-specific leaks here.
- **`backend.py`** — `ForgeBackend` is a `typing.Protocol` (not a base class): the five methods, each returning neutral models. Adding a backend means satisfying this protocol; nothing else in the system changes.
- **`tea_backend.py`** — the only implementation in v1. Isolates the `subprocess` call to `tea`; `_run_tea(args: list[str]) -> Any` is the single choke point. Argv-building and tea-JSON→neutral-model mapping are pure functions, unit-tested against captured tea output with no network. Forge-specific quirks (e.g. filtering `tea actions runs` by branch client-side) live here.
- **`select.py`** — `select_backend(cfg)` returns a backend or `None`. v1: return `TeaBackend()` when the config flag is on and `tea` is available, else `None`. A future gh backend adds one branch here (e.g. by remote host or `gh auth status`).
- **`tools/forge_tools.py`** — the thin, forge-agnostic tool layer. `build_forge_toolset(backend)` defines the five tools closing over `backend`; each calls `backend.<method>` and uses `ctx.deps.workspace` for cwd/current-branch. Model-facing docstrings live here. No new `Deps` field is needed — the backend is bound by closure.

## The five tools (forge-agnostic)

Each is a function inside `build_forge_toolset`, closing over the selected `backend`. Read tools ungated; write/checkout gated.

| Tool | Gated | Backend call | Returns |
|---|---|---|---|
| `list_prs(state="open", limit=30)` | no | `backend.list_prs(state, limit)` | list of neutral `PullRequest` |
| `view_pr(number=None)` | no | `backend.view_pr(number or current_branch)` | one `PullRequest` incl. `ci` + mergeable/review detail; clear "no PR for branch X" if none |
| `ci_status(branch=None, pr=None)` | no | `backend.ci_status(branch or current_branch, pr)` | `CiStatus`: overall + per-run, failing runs first |
| `create_pr(title, body="", base=None, draft=False)` | **yes** | preflight, then `backend.create_pr(...)` (head = current branch) | created `PullRequest` (number, url) |
| `checkout_pr(number, create_branch=True)` | **yes** | `backend.checkout_pr(number, create_branch)` | confirmation of the branch now checked out |

Neutral model shape (`models.py`):

- `PullRequest`: `number` (tea's `index` / gh's `number`), `title`, `state`, `author`, `head`, `base`, `mergeable`, `url`, `updated`, optional `ci: CiStatus | None`.
- `CiRun`: `workflow`, `event`, `status`, `conclusion` (**normalized** to `success`/`failure`/`pending`), `url`.
- `CiStatus`: `overall` (normalized), `runs: list[CiRun]` (failing first).

Design notes:

- **`view_pr` / `ci_status` default to the current git branch** so "what's failing in CI?" works with no args — the common case. Branch resolution is git-level and lives in the tool layer (shared across backends).
- **`create_pr` preflight** (in the tool layer, forge-neutral): verify the current branch is pushed to `origin` and that no open PR already exists for it (via `backend.list_prs`). On failure, return an actionable instruction (e.g. "branch not pushed — run `git push -u origin <branch>` first"). `create_pr` **does not push** — pushing stays an explicit `bash git push`, honoring "commit/push only when asked".
- **`checkout_pr`** mutates the working tree (like `bash`), so it is gated and denied in `plan` mode.
- Results are **compact** (neutral dataclasses → dicts) trimmed to fields that matter, not raw CLI tables, to conserve context.

## Toolset construction, availability & wiring

```python
def build_forge_toolset(backend: ForgeBackend) -> FunctionToolset[Deps]:
    ts = FunctionToolset()
    ts.add_function(list_prs)       # each closes over `backend`
    ts.add_function(view_pr)
    ts.add_function(ci_status)
    ts.add_function(create_pr,   requires_approval=True)
    ts.add_function(checkout_pr, requires_approval=True)
    return ts
```

**One build-time availability decision:** `select_backend(cfg)`. It folds the two conditions that used to be separate gates:

1. `cfg.forge_enabled` — config flag, env `MARIM_FORGE`, default **on**; lets the integration be switched off entirely.
2. Backend availability — for tea: `tea` on PATH **and** a default login exists.

If `select_backend` returns `None`, the toolset is not attached — no per-turn cost, no always-erroring tools.

```python
backend = select_backend(cfg)
forge_ts = build_forge_toolset(backend) if backend else None
agent = Agent(..., toolsets=[forge_ts] if forge_ts else [])
```

Assembling `toolsets` as a constructor arg is a small reorder in `build_collaborators`, not a new mechanism. It composes with the MCP `toolsets=` that flow per-turn into `agent.run` — they are independent.

**Sub-agents:** the forge tools are **not** granted to sub-agents in v1. PR/CI actions belong to the main loop's human-in-the-loop flow; sub-agents run un-gated and would create PRs with no approval. Revisit later if a use case appears (YAGNI now).

**Config surface:** add `forge_enabled: bool` to `HarnessConfig`, threaded from an env read in `bootstrap.build_harness` — the same path `lsp_enabled` takes.

## Command safety & error handling

There is exactly **one** availability decision — `select_backend` at build time. If the tools exist, a backend was selected and its CLI was present/logged-in when the agent was built, so no tool re-checks "is the CLI installed?" at runtime.

**Invocation discipline (`TeaBackend._run_tea`)** — the single choke point:

- Always an **argv list** `["tea", <subcommand>, …, "--output", "json"]`, never a shell string. User-supplied values (title/body/branch) are separate argv elements, so backticks / `$()` in a PR body are inert — no shell-injection surface.
- Run with `cwd = workspace.root` and a timeout (~20s).
- Non-zero exit → raise `ForgeError(stderr)`; the tool catches it and returns the stderr as an actionable message (not a traceback).
- JSON parse failure (CLI version drift) → `ForgeError("could not parse tea output")` including the raw first line, so drift degrades loudly, not silently.

(A future `GhBackend` gets its own equivalent choke point and raises the same `ForgeError`, so the tool layer's error handling is backend-agnostic.)

**Approval semantics:** `create_pr` / `checkout_pr` defer through the normal `resolve_approvals` loop — `auto` runs, `ask` prompts, `plan` denies. In plan mode marim can freely read PR/CI state but cannot open a PR or mutate the tree — the same boundary logic as `net_tools`.

**Runtime failures that CAN happen (toolset present):**

- Per-command failure (network down, token expired mid-session, rate-limited) → CLI exits non-zero → `ForgeError(stderr)` surfaced. Generic; no special "CLI missing" branch.
- Not inside a forge repo / no matching remote → the CLI's own error surfaced clearly.
- `create_pr` preflight (unpushed branch, existing PR) → specific instruction.
- The rare "login removed mid-session" case falls through the same per-command `ForgeError` path — no dedicated guard.

**Secrets:** the CLI reads its own token from its config (`$XDG_CONFIG_HOME/tea` for tea); marim never handles it. Tool results never echo the token.

## Testing

Most coverage is fast and offline, following the pure-core / thin-tool split. Tests target the seam, so a future backend plugs into the same structure:

- **`tests/test_forge_tea_backend.py`** — `TeaBackend` pure functions: argv builders (`create_pr` with draft/base, `list_prs` state/limit) and tea-JSON→neutral-model mappers, driven by **captured real tea JSON fixtures** (capture `tea pr list -o json` and `tea actions runs -o json` once, freeze them). No subprocess, no network. Asserts the neutral-model normalization (tea `index`→`number`, conclusion normalization).
- **`_run_tea` error paths** — monkeypatch the subprocess runner: non-zero exit → `ForgeError(stderr)`; unparseable stdout → `ForgeError`; timeout handling. Assert argv is a list (injection guard).
- **`tests/test_forge_tools.py`** — call each tool with a fake `Deps` and a **stub `ForgeBackend`** (in-memory, no CLI); assert formatted results and both `create_pr` preflight branches (unpushed → instruction, existing PR → refusal). Assert `build_forge_toolset()` marks `create_pr`/`checkout_pr` `requires_approval=True` and the three reads not. The stub-backend pattern is exactly how the tool tests stay backend-agnostic.
- **`tests/test_bootstrap.py`** (addition) — `select_backend` returns `None` ⇒ no toolset attached (CLI absent, or `forge_enabled=False`); returns a backend ⇒ toolset attached. `tea_available()` tested via monkeypatched PATH / logins check.
- **No live-network tests in CI** — CLI calls are always stubbed. An optional manual smoke against git.marim.dev stays out of the suite.

Runs clean under the CI order `ruff → pyright → pytest` on Python 3.10/3.12/3.14 (argv + frozen dataclasses are 3.10-safe).

## Future: adding the `gh` backend

Documented here so the seam's payoff is concrete, but **not implemented in v1**:

1. Add `forge/gh_backend.py` with a `GhBackend` satisfying `ForgeBackend`, shelling out to `gh … --json …` and mapping GitHub's JSON into the same neutral models (GitHub's `number`, `headRefName`/`baseRefName`, `mergeable`, checks via `gh pr checks` / `gh run list` map directly).
2. Add one branch to `select_backend` choosing tea vs gh (e.g. by the `origin` remote host, or which CLI is authenticated).
3. Add `tests/test_forge_gh_backend.py` mirroring the tea backend tests with captured `gh` fixtures.

No change to `models.py`, `backend.py`, `forge_tools.py`, the toolset wiring, gating, or the tool tests — that invariance is the point of the seam.

## Open items for the implementation plan

- Confirm the exact `ci` field shape from `tea pr list --fields …,ci` and the `tea actions runs -o json` schema against captured fixtures; adjust the `CiStatus` mapping and normalization table accordingly.
- Decide the precise structured shape each tool returns to the model (dict vs. formatted string) during implementation — keep it compact either way.
