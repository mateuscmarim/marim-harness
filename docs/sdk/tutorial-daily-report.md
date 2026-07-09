# Tutorial: the daily-report agent

A complete, real embedder built on the SDK: an unattended agent that runs
every evening, reads the day's commits across every repo in a workspace,
infers the *tasks* worked on, and writes a per-day markdown report — with
continuity against the previous two days' reports. It was built specifically
to validate the `HarnessBuilder` surface, so it exercises most of what these
docs describe: a near-bare build, one custom tool, extra instructions,
`Mode.auto`, gated `write_file`, model injection for tests, and headless
operation under systemd.

This page walks through its design decisions in the order you'd make them
for your own embedder.

## Architecture: deterministic shell, one focused turn

The most important decision is what *not* to give the model. The pipeline
is:

```
harvest commits (plain git, no model)
  → assemble one prompt (plain Python)
    → ONE agent turn (infer tasks, write the report via write_file)
      → verify the file exists (plain Python)
```

Everything mechanical — walking repos, running `git log`, computing the
week directory, loading prior reports, deciding the output path — is
deterministic Python. The model does the one thing only a model can do:
read a day of commit messages and turn them into a task narrative. One
turn, one file written, done.

This shape (deterministic harvest → single focused turn → post-turn
verification) is a good default for any unattended embedder: it minimizes
cost, keeps failures diagnosable, and makes the agent's job small enough to
prompt precisely.

## The builder chain

The whole SDK composition is one expression:

```python
def build_reporter_harness(
    project_root: Path,
    workspace: Path,
    repo_names: frozenset[str],
    model: object | None = None,
):
    from marim_harness import HarnessBuilder, Mode

    return (
        HarnessBuilder(workspace=project_root, model=model or DEFAULT_MODEL)
        .with_tool(make_commit_diff_tool(workspace, repo_names))
        .with_instructions(extra=REPORTER_INSTRUCTIONS)
        .with_mode(Mode.auto)
        .build()
    )
```

Reading it against the [builder reference](builder.md):

- **The bare defaults do most of the work.** No `with_defaults()`, no
  `with_sessions()`, no memory/skills/net. The bare build already includes
  file reads plus gated `write_file`/`edit_file` — exactly the reach this
  agent needs, and nothing else. No XDG reads, nothing persisted.
- **`workspace=project_root`**, not the code workspace being reported on.
  The harness workspace is where the agent may *write* (the reports repo);
  the repos being read are reached only through the custom tool, which does
  its own confinement. Choosing the workspace by "what may the model
  mutate" rather than "what is the subject matter" is the right instinct.
- **`with_mode(Mode.auto)`** — explicit, even though `auto` is the default.
  An unattended cron job has nobody to approve prompts, so `ask` would
  deadlock into denials; stating `auto` documents that this is a deliberate
  trust decision, not an accident.
- **`model=` is a seam, not a constant.** `None` means the production
  OpenRouter slug; tests pass a `FunctionModel`; a local smoke run passes a
  constructed `OpenAIChatModel` pointed at LM Studio. Same builder, three
  model kinds — see [Model polymorphism](#model-polymorphism) below.

## The custom tool

The prompt contains commit subjects and bodies, which is usually enough to
infer tasks. For the vague ones, the agent gets exactly one escape hatch —
a read-only `commit_diff` tool, built with the factory-closure pattern from
[Custom tools](custom-tools.md):

```python
def make_commit_diff_tool(workspace: Path, repo_names: frozenset[str]):
    def commit_diff(ctx: RunContext[Deps], repo: str, sha: str) -> str:
        """Show the full diff of one commit when its message is too vague to
        infer the task. `repo` is the repository name from the commit list;
        `sha` is the commit hash."""
        if repo not in repo_names:
            return f"error: unknown repo {repo!r}; valid: {sorted(repo_names)}"
        if not _SHA_RE.match(sha):
            return f"error: invalid sha {sha!r}"
        proc = subprocess.run(
            ["git", "-C", str(workspace / repo), "show", "--stat", "--patch", sha],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return f"error: git show failed: {proc.stderr.strip()}"
        out = proc.stdout
        if len(out) > _DIFF_BUDGET:
            out = out[:_DIFF_BUDGET] + "\n... (truncated)"
        return out

    return commit_diff
```

The details worth copying:

- **Closure over run-scoped state.** The repo allowlist and workspace path
  are fixed at build time; the model cannot point the tool at arbitrary
  directories. No globals, no config lookup inside the tool.
- **Validate model-supplied arguments like untrusted input** — allowlist
  the repo name, regex the sha (which also keeps it shell-safe), bound the
  output (`_DIFF_BUDGET` truncation) so one huge merge commit can't blow
  the context.
- **Error strings, not exceptions**, for every expected failure — the model
  reads `error: unknown repo ...` and corrects course.
- **The docstring tells the model *when*, not just *what*:** "when its
  message is too vague to infer the task" is usage guidance, and it works —
  in live runs the model calls it rarely, exactly as intended.
- Registered **ungated** (`requires_approval` defaults to `False`): it's
  read-only, so gating would add nothing.

And the imports at the top of the module carry the one hard-won lesson:

```python
# These two imports must stay at runtime (NOT under TYPE_CHECKING): pydantic-ai
# resolves the tool's stringified annotations via get_type_hints() at
# registration, which needs both names in this module's real globals.
from marim_harness import Deps
from pydantic_ai import RunContext
```

See [the import gotcha](custom-tools.md#the-import-gotcha-read-this) — this
project is where it was found.

## Instructions vs. prompt

The split follows the cache rule from [Turns](turns.md): **stable policy in
`with_instructions(extra=...)`, per-run data in the prompt.**

The instructions (`REPORTER_INSTRUCTIONS`) define the role once: what a
"task" is (a unit of intent spanning commits — never one task per commit),
when to use `commit_diff`, how to handle continuity with prior reports, and
the write discipline ("write with the write_file tool to exactly the path
you are given... Do not write or edit any other file").

The prompt, assembled fresh each run by `build_prompt`, carries five
elements:

1. **The assignment line** — date, weekday, workspace name.
2. **Today's commits** — grouped per repo: short sha, subject, author,
   body when present, and the one-line change-stat summary.
3. **Prior reports** — the previous two days' full text, framed as "for
   continuity — do not repeat them" (or an explicit "no prior reports
   exist" so the model doesn't go looking).
4. **The exact output path** — relative to the workspace, precomputed by
   the shell, so the model never invents a location.
5. **The markdown template, verbatim** — the model fills a structure, it
   doesn't design one. This is the single biggest lever on output
   consistency, especially for smaller models.

## The wrapper: seams, short-circuits, verification

`__main__.py` is ~80 lines, and three of its moves are worth stealing:

**A one-function seam between CLI and harness.**

```python
def run_agent_turn(project_root, workspace, repo_names, prompt) -> str:
    """Seam for tests: build the harness and drive the single report turn."""
    harness = build_reporter_harness(project_root, workspace, repo_names)
    return asyncio.run(harness.run_turn(prompt))
```

Every CLI test monkeypatches this one name and asserts orchestration
(argument handling, exit codes) without ever building a harness or paying
for a model.

**Short-circuit before spending money.** No commits today → write a
deterministic stub report and exit 0 *without ever constructing the
harness*. Missing API key → exit 1 before the turn, not a mid-turn
provider error.

**Never trust the model's word for a side effect.** After the turn, the
wrapper checks the file:

```python
if not out_path.exists():
    print(f"error: report was not written to {out_path} "
          f"(model said: {reply!r})", file=sys.stderr)
    return 1
```

`run_turn` returns the model's *text*; whether the gated write actually
happened is a filesystem fact. For an unattended agent, post-turn
verification of the intended side effect is the difference between a cron
job that fails loudly and one that silently emails you nothing for a week.

## Testing

27 tests, none needing a network or key. The centerpiece is the end-to-end
proof that the SDK wiring holds — a `FunctionModel` scripted to call the
*real* gated `write_file`:

```python
async def test_harness_turn_writes_report_via_gated_write(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    relpath = "reports/2026-W28/2026-07-08.md"

    def script(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="write_file",
                args={"path": relpath, "content": "# Daily report\n\nok\n"},
            )])
        return ModelResponse(parts=[TextPart("report written")])

    harness = build_reporter_harness(
        project_root=project, workspace=tmp_path,
        repo_names=frozenset({"twm"}), model=FunctionModel(script),
    )
    out = await harness.run_turn("write the report")
    assert out == "report written"
    assert (project / relpath).read_text().startswith("# Daily report")
```

That one test covers builder composition, the deferred-approval loop,
`Mode.auto` resolution, path confinement, and the write itself. A sibling
test scripts a `commit_diff` call against a real throwaway git repo to
prove the custom tool is reachable through the same path. The rest of the
suite is plain unit tests on the deterministic shell (harvest, report fs,
prompt assembly, CLI orchestration via the monkeypatched seam). See
[Testing](testing.md) for the patterns in isolation.

## Model polymorphism

Because the builder takes either a model string or a constructed pydantic-ai
`Model`, the same `build_reporter_harness` ran, unmodified, against:

- **Production:** the `DEFAULT_MODEL` OpenRouter slug (string).
- **Tests:** `FunctionModel(script)` — scripted turns, free, offline.
- **Local smoke:** an LM Studio model, proving the whole pipeline
  end-to-end for free before spending provider credits:

```python
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

model = OpenAIChatModel(
    "ornith-1.0-9b",
    provider=OpenAIProvider(base_url="http://localhost:1234/v1",
                            api_key="lm-studio"),
)
harness = build_reporter_harness(project_root, workspace, repo_names, model=model)
```

The local smoke run harvested 115 real commits across 5 repos and produced
a valid report through the gated write path — the same code that runs in
production. Design your embedder's model parameter as this kind of seam
from day one.

## Scheduling

Headless operation is just the CLI under a systemd user timer:

```ini
# ~/.config/systemd/user/daily-report.service
[Service]
Type=oneshot
EnvironmentFile=%h/.config/daily-report/env   # OPENROUTER_API_KEY, chmod 600
ExecStart=/usr/bin/env uv run daily-report

# ~/.config/systemd/user/daily-report.timer
[Timer]
OnCalendar=*-*-* 19:00 America/Sao_Paulo
Persistent=true
```

Notes that generalize:

- The key lives in an `EnvironmentFile` with `600` perms — never in the
  unit file or shell history.
- `Persistent=true` catches up a missed run at next boot; the exit-code
  contract (0 = report on disk, 1 = anything else) makes
  `systemctl --user status` meaningful.
- The workspace is a git repo, so `.marim/` is in its `.gitignore` — the
  [provider-error spill](sessions-and-state.md#the-marim-spill) showed up
  on the very first hard provider failure, exactly as documented.

## What to take away

For your own embedder, the checklist this project validates:

1. Keep the model's job minimal; do everything deterministic in Python.
2. Start from the bare build; add only the groups you need.
3. Pick `workspace` by write-reach, not subject matter.
4. Custom tools: factory closures, untrusted-input validation, error
   strings, docstrings that say *when*.
5. Stable policy in instructions, per-run data in the prompt, exact paths
   and templates supplied — never inferred.
6. `model=` as an injection seam: string in production, `FunctionModel` in
   tests, local models for free smoke runs.
7. Verify the side effect after the turn; don't trust the reply text.
8. One scripted end-to-end test through the real gated path.
