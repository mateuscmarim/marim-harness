# Custom tools

A custom tool registers exactly like a built-in one — same signature shape,
same approval path when gated. `_ComposedProvider` registers your tools on
the same pydantic-ai agent the built-ins use; there is no second-class
plugin mechanism.

## Shape

```python
from pydantic_ai import RunContext
from marim_harness import Deps


def deploy(ctx: RunContext[Deps], target: str) -> str:
    """Deploy the app to `target`."""
    return f"deployed {target}"


harness = (
    HarnessBuilder(workspace=Path("."), model="anthropic:claude-sonnet-4-6")
    .with_tool(deploy, requires_approval=True)
    .build()
)
```

- **First parameter:** `ctx: RunContext[Deps]`. `Deps` is the per-turn
  payload threaded through every tool — `ctx.deps.workspace.root` is the
  workspace path, `ctx.deps.workspace.mode` the current mode. Import it from
  the package root: `from marim_harness import Deps`.
- **Docstring = tool description.** The model reads it to decide when and
  how to call the tool. Write it as product copy, not an implementation
  note: say what the tool does, what the arguments mean, and when to use it.
- **Remaining parameters** become the tool's schema via pydantic-ai —
  annotate them precisely; the model sees the types.
- **Return a string** (or something pydantic-ai can serialize). For failure
  cases a tool *expected* to hit (bad input, missing resource), return an
  error string rather than raising — the model can read it and correct
  course. Reserve exceptions for genuine bugs.
- **Async tools work too** — `async def` is registered the same way.

## The import gotcha (read this)

Both `RunContext` and `Deps` must be **real runtime imports** — do not move
them under an `if TYPE_CHECKING:` block. pydantic-ai resolves the tool's
annotations with `get_type_hints()` at registration time, and under
`from __future__ import annotations` every annotation is a string that must
resolve against your module's actual globals. TYPE_CHECKING-only imports
fail at `build()` with `NameError` — not at type-check time, and not with a
message that points here. (Found the hard way by the first real embedder;
its reviewer reproduced the failure to confirm.)

## Gating

`requires_approval=True` routes the call through the approval loop against
the current `Mode`, exactly like `write_file`/`edit_file`/`bash`:

- `auto` runs it unprompted,
- `plan` denies it,
- `ask` delegates to the approval callback wired via `Harness.bind_ui` —
  with none wired (the common headless case), `ask` denies every gated call.

Gate anything that mutates state or reaches outside the workspace. Leave
read-only tools ungated (`requires_approval=False`, the default) so plan
mode and ask mode stay usable.

## Validation at `build()`

- A custom tool whose name collides with any tool actually loaded fails
  `build()` — that includes built-ins from enabled groups, the six LSP names
  when `with_lsp(tools=True)`, and the five forge names under
  `with_forge(...)`. The same name is fine when the colliding group is off.
- Registering the same custom tool name twice fails `build()`.
- MCP tool names are the accepted gap (servers connect after `build()`); a
  collision there surfaces at connect/run time.

## A real example

The daily-report embedder's `commit_diff` tool is a good template for a
read-only tool with untrusted-ish inputs — it validates a repo name against
an allowlist, validates a sha against a regex, truncates output to a fixed
budget, and returns error strings instead of raising:

```python
def make_commit_diff_tool(workspace: Path, repo_names: frozenset[str]):
    def commit_diff(ctx: RunContext[Deps], repo: str, sha: str) -> str:
        """Show the full diff of one commit in one of today's repos.

        Use only when a commit's message is too vague to infer what it did.
        `repo` must be one of the repo names listed in the prompt.
        """
        if repo not in repo_names:
            return f"error: unknown repo {repo!r}"
        if not _SHA_RE.match(sha):
            return f"error: {sha!r} does not look like a commit sha"
        ...
    return commit_diff
```

The factory-closure pattern (`make_*_tool`) is how you parameterize a tool
with run-scoped state without globals; register the returned function with
`with_tool`. See the [tutorial](tutorial-daily-report.md) for the full
context.
