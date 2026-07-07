# Gitea integration via `tea` — a Pydantic AI toolset

**Date:** 2026-07-07
**Status:** Design approved, ready for implementation plan
**Scope:** Agent-facing tools for Gitea pull requests + CI/actions, delivered as a Pydantic AI `FunctionToolset` that shells out to the `tea` CLI. A read-only TUI surface is explicitly **out of scope** (follow-up spec).

## Goal

Give marim a first-class, agent-facing capability to work with the project's Gitea forge (`git.marim.dev`) the way Claude Code leans on `gh`: open a PR for the current branch, inspect PRs, and answer "what's failing in CI?" — without dropping to raw `bash`. The capability is a set of **dedicated, typed tools** grouped into one Pydantic AI toolset, not free-form CLI guidance.

### Non-goals (v1)

- Issues, comments/reviews, labels, milestones, releases — deferred.
- Any TUI surface (CI status indicator, PR panel) — separate follow-up spec.
- Granting the tea tools to sub-agents — main-loop only in v1.
- Managing tea auth/config — tea is already logged in (`git-marim-dev`, default login); the toolset relies on tea's own config.

## Context

- `tea` v0.14.2 is installed at `/usr/bin/tea`; the repo remote is `https://git.marim.dev/mateuscmarim/marim-harness.git`.
- Every relevant tea subcommand supports `--output json` and PR listing supports `--fields` selection (including a `mergeable` field and a `ci` status field), so structured parsing is robust rather than table-scraping.
- marim registers builtin tools via `provider.register(agent)` at construction and passes MCP tools as `toolsets` into `agent.run` per turn. Pydantic AI (1.107.0) `FunctionToolset.add_function`/`.tool` accept `requires_approval`, so one toolset can hold ungated reads and gated writes.
- marim gates approval **per tool**: read tools are ungated, mutating/outbound tools use `requires_approval=True` and defer through `resolve_approvals` (`auto` runs, `ask` prompts, `plan` denies).

## Architecture

New self-contained subsystem, following marim's pure-core / thin-tool split (mirrors `lsp/`, `mcp/`):

```
src/marim_harness/gitea/
  __init__.py
  tea_cli.py      # side-effectful tea invocation in ONE place (_run_tea);
                  #  pure argv-builders and JSON→dataclass mappers;
                  #  tea_available() (PATH + default login) for the build-time gate
  models.py       # frozen dataclasses: PullRequest, CiRun, CiStatus (+ TeaError)
src/marim_harness/tools/gitea_tools.py
                  # the 5 tool functions + build_gitea_toolset() -> FunctionToolset
```

- **`tea_cli.py`** isolates the `subprocess` call. `_run_tea(args: list[str]) -> Any` is the single choke point; argv-building and JSON→dataclass mapping are pure functions, unit-tested against captured tea output with no network.
- **`gitea_tools.py`** is the thin tool layer. Each tool reads `ctx.deps`, calls `tea_cli`, and returns a compact structured/string result. Model-facing docstrings live here alongside marim's other tool docstrings. `build_gitea_toolset()` assembles the `FunctionToolset` with per-tool gating.
- Wiring lives in `build_collaborators` (`runtime/harness.py`), gated behind a config flag + `tea_available()`.

## The five tools

Each is a plain module-level function. Read tools are ungated; write/checkout tools are gated.

| Tool | Gated | tea invocation | Returns |
|---|---|---|---|
| `list_prs(state="open", limit=30)` | no | `tea pr list -o json --fields index,title,state,author,head,base,mergeable,url,updated` | list of `PullRequest` |
| `view_pr(number=None)` | no | `tea pr list` filtered to `number`; `number=None` resolves the PR whose `head` == current git branch | one `PullRequest` incl. `ci` + mergeable/review detail; clear "no PR for branch X" if none |
| `ci_status(branch=None, pr=None)` | no | `tea actions runs -o json`, filtered client-side by branch (or the PR's head); fallback to PR `ci` field | `CiStatus`: overall + per-run (workflow, event, status, conclusion, url), failing runs first |
| `create_pr(title, body="", base=None, draft=False)` | **yes** | `tea pr create --title … --description … [--base …] [--draft]` (head = current branch) | created `PullRequest` (number, url) |
| `checkout_pr(number, create_branch=True)` | **yes** | `tea pr checkout <number> -b` | confirmation of the branch now checked out |

Design notes:

- **`view_pr` / `ci_status` default to the current git branch** so "what's failing in CI?" works with no args — the common case.
- **`create_pr` preflight** (before shelling out): verify the current branch is pushed to `origin` and that no open PR already exists for it (via `list_prs`). On failure, return an actionable instruction (e.g. "branch not pushed — run `git push -u origin <branch>` first") instead of a confusing tea error. `create_pr` **does not push** — pushing stays an explicit `bash git push`, honoring "commit/push only when asked".
- **`checkout_pr`** mutates the working tree (like `bash`), so it is gated and denied in `plan` mode.
- Results are **compact** dataclasses→dicts trimmed to fields that matter, not raw tea tables, to conserve context.

## Toolset construction, availability & wiring

```python
def build_gitea_toolset() -> FunctionToolset[Deps]:
    ts = FunctionToolset()
    ts.add_function(list_prs)
    ts.add_function(view_pr)
    ts.add_function(ci_status)
    ts.add_function(create_pr,   requires_approval=True)
    ts.add_function(checkout_pr, requires_approval=True)
    return ts
```

**Two independent gates** (mirroring `lsp_enabled`):

1. `cfg.gitea_enabled` — config flag, env `MARIM_GITEA`, default **on**; lets the integration be switched off entirely.
2. `tea_available()` — `tea` on PATH **and** a default login exists.

Both are evaluated **once at build time** in `build_collaborators`. If either is false, the toolset is simply not attached — no per-turn cost, no always-erroring tools.

```python
gitea_ts = build_gitea_toolset() if (cfg.gitea_enabled and tea_available()) else None
agent = Agent(..., toolsets=[gitea_ts] if gitea_ts else [])
```

Assembling `toolsets` as a constructor arg is a small reorder in `build_collaborators`, not a new mechanism. It composes with the MCP `toolsets=` that flow per-turn into `agent.run` — they are independent.

**Sub-agents:** the tea tools are **not** granted to sub-agents in v1. PR/CI actions belong to the main loop's human-in-the-loop flow; sub-agents run un-gated and would create PRs with no approval. Revisit later if a use case appears (YAGNI now).

**Config surface:** add `gitea_enabled: bool` to `HarnessConfig`, threaded from an env read in `bootstrap.build_harness` — the same path `lsp_enabled` takes.

## Command safety & error handling

There is exactly **one** availability decision — the build-time gate. If the tools exist, tea was present and logged in when the agent was built, so no tool re-checks "is tea installed?" at runtime.

**Invocation discipline (`_run_tea`)** — the single choke point:

- Always an **argv list** `["tea", <subcommand>, …, "--output", "json"]`, never a shell string. User-supplied values (title/body/branch) are separate argv elements, so backticks / `$()` in a PR body are inert — no shell-injection surface.
- Run with `cwd = workspace.root` and a timeout (~20s).
- Non-zero exit → raise `TeaError(stderr)`; the tool catches it and returns the stderr as an actionable message (not a traceback).
- JSON parse failure (tea version drift) → `TeaError("could not parse tea output")` including the raw first line, so drift degrades loudly, not silently.

**Approval semantics:** `create_pr` / `checkout_pr` defer through the normal `resolve_approvals` loop — `auto` runs, `ask` prompts, `plan` denies. In plan mode marim can freely read PR/CI state but cannot open a PR or mutate the tree — the same boundary logic as `net_tools`.

**Runtime failures that CAN happen (toolset present):**

- Per-command failure (network down, token expired mid-session, rate-limited) → tea exits non-zero → `TeaError(stderr)` surfaced. Generic; no special "tea missing" branch.
- Not inside a Gitea repo / no matching remote → tea's own error surfaced clearly.
- `create_pr` preflight (unpushed branch, existing PR) → specific instruction.
- The rare "login removed mid-session" case falls through the same per-command `TeaError` path — no dedicated guard.

**Secrets:** tea reads its own token from `$XDG_CONFIG_HOME/tea`; marim never handles it. Tool results never echo the token.

## Testing

Most coverage is fast and offline, following the pure-core / thin-tool split:

- **`tests/test_gitea_tea_cli.py`** — pure functions: argv builders (`create_pr` with draft/base, `list_prs` state/limit) and JSON→dataclass mappers, driven by **captured real tea JSON fixtures** (capture `tea pr list -o json` and `tea actions runs -o json` once, freeze them). No subprocess, no network.
- **`_run_tea` error paths** — monkeypatch the subprocess runner: non-zero exit → `TeaError(stderr)`; unparseable stdout → `TeaError`; timeout handling. Assert argv is a list (injection guard).
- **`tests/test_gitea_tools.py`** — call each tool with a fake `Deps` and a stubbed `tea_cli`; assert formatted results and both `create_pr` preflight branches (unpushed → instruction, existing PR → refusal). Assert `build_gitea_toolset()` marks `create_pr`/`checkout_pr` `requires_approval=True` and the three reads not.
- **`tests/test_bootstrap.py`** (addition) — `tea_available()` false ⇒ no toolset attached; `gitea_enabled=False` ⇒ same. `tea_available()` itself tested via monkeypatched PATH / logins check.
- **No live-network tests in CI** — tea calls are always stubbed. An optional manual smoke against git.marim.dev stays out of the suite.

Runs clean under the CI order `ruff → pyright → pytest` on Python 3.10/3.12/3.14 (argv + frozen dataclasses are 3.10-safe).

## Open items for the implementation plan

- Confirm the exact `ci` field shape returned by `tea pr list --fields …,ci` and the `tea actions runs -o json` schema against captured fixtures; adjust `CiStatus` mapping accordingly.
- Decide the precise structured shape each tool returns to the model (dict vs. formatted string) during implementation — keep it compact either way.
