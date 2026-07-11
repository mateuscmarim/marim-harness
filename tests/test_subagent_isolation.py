"""Worktree isolation for spawns: an isolated sub-agent runs in its own git
worktree so parallel mutating spawns can't clobber each other or the main tree;
its changes are committed to a branch and reported back, and the worktree is
cleaned up."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage

from marim_harness.tools.impl import fs
from tests.conftest import _last_instructions, _make_deps, _make_harness, _text_model


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with one commit, on branch main."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _capture_deps(h):
    """Stub build so the spawned agent records the deps it ran with."""
    cap: dict = {}

    class _StubAgent:
        async def run(self, task, **kwargs):
            cap["workspace_root"] = kwargs["deps"].workspace.root
            return SimpleNamespace(output="ok", usage=RunUsage(), all_messages=list)

    h.subagents.build = lambda type, max_output_chars=None, model=None, \
        workspace_root=None, defn=None, depth=0, mask_trigger=None, \
        checkpoint=None: (_StubAgent(), None)
    return cap


@pytest.mark.anyio
async def test_isolated_spawn_runs_in_a_worktree(repo: Path):
    deps = _make_deps(repo)
    h = _make_harness(_text_model(), deps)
    cap = _capture_deps(h)

    await h.subagents.run("general", "do it", "tc1", isolation="worktree")
    ran_in = cap["workspace_root"]
    assert ran_in != repo
    assert ".worktrees" in str(ran_in)


@pytest.mark.anyio
async def test_non_isolated_spawn_runs_in_main_workspace(repo: Path):
    deps = _make_deps(repo)
    h = _make_harness(_text_model(), deps)
    cap = _capture_deps(h)

    await h.subagents.run("general", "do it", "tc1")
    assert cap["workspace_root"] == repo
    assert not (repo / ".worktrees").exists()


@pytest.mark.anyio
async def test_isolated_spawn_commits_changes_and_reports_branch(repo: Path):
    """A sub-agent that writes a file in its worktree gets that change committed
    to a branch; the report names the branch and the worktree is torn down."""
    deps = _make_deps(repo)
    h = _make_harness(_text_model(), deps)

    class _WritingAgent:
        async def run(self, task, **kwargs):
            root = kwargs["deps"].workspace.root
            fs.write_file(root, "new.txt", "from sub-agent\n")
            return SimpleNamespace(output="wrote new.txt", usage=RunUsage(), all_messages=list)

    h.subagents.build = lambda type, max_output_chars=None, model=None, \
        workspace_root=None, defn=None, depth=0, mask_trigger=None, \
        checkpoint=None: (_WritingAgent(), None)

    out = await h.subagents.run("general", "add a file", "tc1", isolation="worktree")
    assert "wrote new.txt" in out
    assert "subagent/tc1" in out          # branch named in the report
    assert "new.txt" in out               # diffstat included
    # The worktree is cleaned up, but the branch carries the committed change.
    assert not (repo / ".worktrees" / "subagent" / "tc1").exists()
    show = subprocess.run(
        ["git", "show", "--stat", "subagent/tc1"], cwd=repo,
        capture_output=True, text=True,
    )
    assert show.returncode == 0
    assert "new.txt" in show.stdout


@pytest.mark.anyio
async def test_isolation_requires_a_git_repo(tmp_path: Path):
    """Outside a git repo, an isolated spawn fails fast with a clear message and
    never runs the sub-agent."""
    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)

    def _no_build(*a, **k):
        raise AssertionError("sub-agent must not be built without a worktree")

    h.subagents.build = _no_build
    out = await h.subagents.run("general", "do it", "tc1", isolation="worktree")
    assert "git" in out.lower()


@pytest.mark.anyio
async def test_isolated_spawn_instructions_point_at_worktree(repo: Path):
    """The sub-agent's instructions describe the worktree path it works in, not
    the main root — so its relative-path reasoning matches where its tools act."""
    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        return ModelResponse(parts=[TextPart(content="done")])

    deps = _make_deps(repo)
    h = _make_harness(FunctionModel(fn), deps)

    await h.subagents.run("general", "do it", "tc1", isolation="worktree")
    assert ".worktrees" in captured["instructions"]


@pytest.mark.anyio
async def test_spawn_agent_tool_forwards_isolation(repo: Path):
    """The spawn_agent tool passes isolation down to the foreground runner."""
    from pydantic_ai.messages import ToolCallPart

    captured: dict = {}

    async def fake_run(type, task, stream_id, mcp_names=None,
                       max_output_chars=None, model=None, isolation=None,
                       caller_depth: int = 0):
        captured["isolation"] = isolation
        return "REPORT"

    def main(messages, info):
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart" and \
                        getattr(p, "tool_name", "") == "spawn_agent":
                    return ModelResponse(parts=[TextPart(content="done")])
        return ModelResponse(parts=[ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "general", "task": "x", "isolation": "worktree"},
        )])

    deps = _make_deps(repo)
    h = _make_harness(FunctionModel(main), deps)
    h.deps.services.run_subagent = fake_run
    await h.run_turn("go")
    assert captured["isolation"] == "worktree"


def _branch_exists(repo: Path, branch: str) -> bool:
    out = subprocess.run(["git", "branch", "--list", branch], cwd=repo,
                         capture_output=True, text=True).stdout
    return out.strip() != ""


@pytest.mark.anyio
async def test_isolated_spawn_no_changes_drops_branch(repo: Path):
    """An isolated spawn that changes no files leaves nothing behind — its empty
    branch is deleted and the worktree removed, so spawns don't accrete branches."""
    deps = _make_deps(repo)
    h = _make_harness(_text_model(), deps)
    _capture_deps(h)  # stub agent writes nothing

    out = await h.subagents.run("general", "noop", "tc1", isolation="worktree")
    assert "no file changes" in out
    assert not _branch_exists(repo, "subagent/tc1")
    assert not (repo / ".worktrees" / "subagent" / "tc1").exists()


@pytest.mark.anyio
async def test_isolated_spawn_crash_cleans_up_worktree_and_branch(repo: Path):
    """A crashed isolated spawn discards its partial work: the (dirty) worktree is
    force-removed and the branch deleted, while the crash stays contained."""
    deps = _make_deps(repo)
    h = _make_harness(_text_model(), deps)

    class _CrashAgent:
        async def run(self, task, **kwargs):
            fs.write_file(kwargs["deps"].workspace.root, "partial.txt", "half\n")
            raise RuntimeError("boom mid-run")

    h.subagents.build = lambda type, max_output_chars=None, model=None, \
        workspace_root=None, defn=None, depth=0, mask_trigger=None, \
        checkpoint=None: (_CrashAgent(), None)

    out = await h.subagents.run("general", "do it", "tc1", isolation="worktree")
    assert "boom" in out  # contained, not raised
    assert not _branch_exists(repo, "subagent/tc1")
    assert not (repo / ".worktrees" / "subagent" / "tc1").exists()


def _crash_build(h):
    """Stub build so the spawned agent always crashes mid-run."""
    class _CrashAgent:
        async def run(self, task, **kwargs):
            raise RuntimeError("boom mid-run")

    h.subagents.build = lambda type, max_output_chars=None, model=None, \
        workspace_root=None, defn=None, depth=0, mask_trigger=None, \
        checkpoint=None: (_CrashAgent(), None)


def _dangling_resume_history():
    from pydantic_ai.messages import ModelRequest, ToolCallPart, UserPromptPart

    return [
        ModelRequest(parts=[UserPromptPart(content="original task")]),
        ModelResponse(parts=[ToolCallPart(
            tool_name="read_file", args={"path": "x"}, tool_call_id="d")]),
    ]


@pytest.mark.anyio
async def test_failed_resumed_isolated_spawn_keeps_its_branch(repo: Path):
    """A resumed isolated spawn's branch holds prior committed work; a failed resume
    must tear down only the worktree checkout and KEEP the branch (history is not
    None → _teardown_worktree(force=True)), so the deliverable isn't destroyed."""
    from marim_harness.session import SessionStore, TranscriptStore

    subprocess.run(["git", "branch", "subagent/sg6"], cwd=repo, check=True)
    assert _branch_exists(repo, "subagent/sg6")

    store = SessionStore(path=repo / "sessions" / "s.json", workspace_root=repo,
                         session_id="s", name="s")
    deps = _make_deps(repo)
    h = _make_harness(_text_model(), deps, store=store)
    _crash_build(h)

    ts = TranscriptStore(store.path, store.session_id)
    ts.write("sg6", _dangling_resume_history(), 2000, meta={
        "stream_id": "sg6", "type": "general", "task": "t", "model": None,
        "mcp": None, "depth": 1, "max_output_chars": None,
        "isolation": "subagent/sg6", "status": "running",
    })

    job_id, message = await h.subagents.resume_spawn("sg6")
    assert job_id is not None, message
    await h.deps.jobs.wait(job_id)
    assert h.deps.jobs.get(job_id).status == "failed"
    assert _branch_exists(repo, "subagent/sg6")  # the deliverable survives


@pytest.mark.anyio
async def test_failed_fresh_isolated_background_spawn_drops_branch(repo: Path):
    """A FRESH background isolated spawn (history is None) that crashes still drops
    its throwaway branch — the resume-only keep-branch path must not leak into it."""
    deps = _make_deps(repo)
    h = _make_harness(_text_model(), deps)
    _crash_build(h)

    with pytest.raises(RuntimeError):
        await h.subagents.run_background(
            "general", "do it", isolation="worktree", stream_id="frb6",
        )
    assert not _branch_exists(repo, "subagent/frb6")


@pytest.mark.anyio
async def test_isolated_spawn_cancel_preserves_in_progress_work(repo: Path):
    """A cancelled isolated spawn with in-progress work (CancelledError is a
    BaseException, e.g. a Ctrl-C tearing down a running spawn) must PRESERVE its
    branch so the run stays resumable — a graceful cancel must lose no more than a
    hard `kill -9`, which leaves the worktree intact. The cancellation still
    propagates (it is not contained as an error string), and the worktree checkout
    is torn down, but the committed work lives on the branch."""
    import asyncio

    deps = _make_deps(repo)
    h = _make_harness(_text_model(), deps)

    class _CancelAgent:
        async def run(self, task, **kwargs):
            fs.write_file(kwargs["deps"].workspace.root, "partial.txt", "half\n")
            raise asyncio.CancelledError

    h.subagents.build = lambda type, max_output_chars=None, model=None, \
        workspace_root=None, defn=None, depth=0, mask_trigger=None, \
        checkpoint=None: (_CancelAgent(), None)

    with pytest.raises(asyncio.CancelledError):
        await h.subagents.run("general", "do it", "tc1", isolation="worktree")
    # The checkout is gone, but the branch survives with the in-progress work
    # committed — reopen() can resume it (contrast the crash path, which discards).
    assert not (repo / ".worktrees" / "subagent" / "tc1").exists()
    assert _branch_exists(repo, "subagent/tc1")
    show = subprocess.run(
        ["git", "show", "--stat", "subagent/tc1"], cwd=repo,
        capture_output=True, text=True,
    )
    assert show.returncode == 0
    assert "partial.txt" in show.stdout


@pytest.mark.anyio
async def test_isolated_spawn_cancel_with_no_changes_drops_branch(repo: Path):
    """The other half of the cancel policy: a genuinely-throwaway spawn that was
    cancelled before changing anything must leave NO dead branch behind — close()
    drops an empty branch, so preserving work never accretes junk branches."""
    import asyncio

    deps = _make_deps(repo)
    h = _make_harness(_text_model(), deps)

    class _CancelAgent:
        async def run(self, task, **kwargs):
            raise asyncio.CancelledError

    h.subagents.build = lambda type, max_output_chars=None, model=None, \
        workspace_root=None, defn=None, depth=0, mask_trigger=None, \
        checkpoint=None: (_CancelAgent(), None)

    with pytest.raises(asyncio.CancelledError):
        await h.subagents.run("general", "do it", "tc1", isolation="worktree")
    assert not _branch_exists(repo, "subagent/tc1")
    assert not (repo / ".worktrees" / "subagent" / "tc1").exists()


@pytest.mark.anyio
async def test_isolated_background_spawn_cancel_preserves_in_progress_work(repo: Path):
    """The background twin of the foreground cancel policy: a cancelled background
    isolated spawn (e.g. cancel_all() tearing down jobs on shutdown) must PRESERVE its
    branch with the in-progress work committed so it stays resumable — discarding would
    lose more than a hard kill and break the still-"running" sidecar's resume offer."""
    import asyncio

    deps = _make_deps(repo)
    h = _make_harness(_text_model(), deps)

    class _CancelAgent:
        async def run(self, task, **kwargs):
            fs.write_file(kwargs["deps"].workspace.root, "partial.txt", "half\n")
            raise asyncio.CancelledError

    h.subagents.build = lambda type, max_output_chars=None, model=None, \
        workspace_root=None, defn=None, depth=0, mask_trigger=None, \
        checkpoint=None: (_CancelAgent(), None)

    with pytest.raises(asyncio.CancelledError):
        await h.subagents.run_background(
            "general", "do it", isolation="worktree", stream_id="bgc6",
        )
    # Checkout gone, branch kept with the committed work — reopen() can resume it.
    assert not (repo / ".worktrees" / "subagent" / "bgc6").exists()
    assert _branch_exists(repo, "subagent/bgc6")
    show = subprocess.run(
        ["git", "show", "--stat", "subagent/bgc6"], cwd=repo,
        capture_output=True, text=True,
    )
    assert show.returncode == 0 and "partial.txt" in show.stdout


@pytest.mark.anyio
async def test_isolated_cli_spawn_cancel_preserves_in_progress_work(repo: Path):
    """A cancelled isolated ``claude-cli`` spawn must KEEP its branch, exactly like
    the native path — it historically ``discard()``ed here, dropping the branch and
    breaking the still-"running" sidecar's own resume offer (parallel-agents review
    finding). Now the CLI path rides the shared ``_run_spawn_lifecycle``, so a
    graceful cancel ``close()``s the worktree: in-progress work is committed and the
    branch survives as the resumable deliverable."""
    import asyncio

    agents = repo / ".marim" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "cli-worker.md").write_text(
        "---\ndescription: CLI worker\nbackend: claude-cli\ntools: read_file\n---\n"
        "You are a CLI worker.\n",
        encoding="utf-8",
    )
    deps = _make_deps(repo)
    h = _make_harness(_text_model(), deps)

    async def _cancel_cli(defn, task, work_root, model, stream_id,
                          checkpoint=None, resume_session_id=None):
        # Produce work in the worktree, then cancel mid-run (BaseException).
        fs.write_file(work_root, "partial.txt", "half\n")
        raise asyncio.CancelledError

    h.subagents._cli.run_cli = _cancel_cli

    with pytest.raises(asyncio.CancelledError):
        await h.subagents.run_background(
            "cli-worker", "do it", isolation="worktree", stream_id="clic6",
        )
    # Checkout gone, branch kept with the committed work — reopen() can resume it.
    # (Before the fix this branch was discarded.)
    assert not (repo / ".worktrees" / "subagent" / "clic6").exists()
    assert _branch_exists(repo, "subagent/clic6")
    show = subprocess.run(
        ["git", "show", "--stat", "subagent/clic6"], cwd=repo,
        capture_output=True, text=True,
    )
    assert show.returncode == 0 and "partial.txt" in show.stdout
