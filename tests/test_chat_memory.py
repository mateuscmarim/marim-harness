"""Chat sessions share project memory without remapping global memory."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

import marim_harness.runtime.bootstrap as bootstrap
import marim_harness.server.supervisor as supervisor_mod
from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.instructions import _memory_index_block
from marim_harness.runtime.permissions import Mode
from marim_harness.server.supervisor import SessionSupervisor
from marim_harness.server.workspaces import WorkspaceRegistry
from marim_harness.session import SessionManager
from marim_harness.tools import memory_tools
from marim_harness.tools.provider import BuiltinToolProvider
from marim_harness.workspace import memory


def test_default_harness_factory_forwards_project_memory_root(monkeypatch):
    captured = {}

    class FakeSession:
        history = []

    class FakeHarness:
        session = FakeSession()

        async def connect(self):
            pass

        async def session_start(self, kind):
            pass

    def fake_build_harness(
        workspace,
        *,
        mode=None,
        resume=False,
        session_id=None,
        project_memory_root=None,
    ):
        captured["project_memory_root"] = project_memory_root
        return FakeHarness()

    monkeypatch.setattr(bootstrap, "build_harness", fake_build_harness)
    shared = Path("/tmp/chatmem")
    asyncio.run(
        supervisor_mod.default_harness_factory(Path("."), "s1", None, project_memory_root=shared)
    )
    assert captured["project_memory_root"] == shared


def test_build_harness_sets_project_memory_root(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setattr(bootstrap.ModelSource, "build", lambda self, model_id: TestModel())
    monkeypatch.setattr(bootstrap, "make_titler", lambda model: None)

    shared = tmp_path / "chat-memory"
    harness = bootstrap.build_harness(tmp_path / "ws", mode=Mode.ask, project_memory_root=shared)

    assert harness.deps.workspace.project_memory_root == shared


def _reply_model() -> FunctionModel:
    def reply(messages, info):
        return ModelResponse(parts=[TextPart(content="done")])

    return FunctionModel(reply)


def test_supervisor_remaps_chat_memory_dir_only_for_chat_workspaces(tmp_path: Path):
    captured = {}

    async def factory(
        workspace: Path,
        session_id: str,
        mode: Mode | None,
        project_memory_root: Path | None = None,
    ) -> Harness:
        captured[workspace.name] = project_memory_root
        manager = SessionManager(workspace)
        return Harness(
            model=_reply_model(),
            provider=BuiltinToolProvider(),
            deps=Deps(
                workspace=WorkspaceConfig(root=workspace, mode=mode or Mode.auto),
                ui=UIHooks(),
            ),
            instructions="You are a coding agent.",
            store=manager.store(session_id),
            manager=manager,
        )

    async def exercise() -> None:
        registry = WorkspaceRegistry(tmp_path / "workspaces.json", tmp_path / "workspaces")
        chat = registry.create_managed("chat", chat=True)
        managed = registry.create_managed("managed")
        for record in (chat, managed):
            SessionManager(Path(record.path)).store("s1").save([], RunUsage())
        shared = tmp_path / "chat-memory"
        supervisor = SessionSupervisor(factory, chat_memory_dir=shared)
        try:
            await supervisor.host_for(chat, "s1")
            await supervisor.host_for(managed, "s1")
        finally:
            await supervisor.aclose()
        assert captured[Path(chat.path).name] == shared
        assert captured[Path(managed.path).name] is None

    asyncio.run(exercise())


def test_project_memory_override_drives_tools_and_instruction_preload(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    workspace = tmp_path / "workspace"
    shared = tmp_path / "chat-memory"
    default_project = memory.project_scope(workspace)
    user_global = memory.global_scope()

    memory.save_memory(
        user_global,
        name="Global fact",
        description="global stays global",
        mem_type="user",
        body="global",
        title="Global fact",
    )
    memory.save_memory(
        default_project,
        name="Hidden project fact",
        description="the workspace-local project index is replaced",
        mem_type="project",
        body="hidden",
        title="Hidden project fact",
    )
    memory.save_memory(
        memory.MemoryScope("project", shared),
        name="Shared chat fact",
        description="chat sessions preload this shared project index",
        mem_type="project",
        body="shared",
        title="Shared chat fact",
    )

    ctx = SimpleNamespace(
        deps=Deps(workspace=WorkspaceConfig(root=workspace, project_memory_root=shared))
    )
    assert memory_tools.resolve_scope(ctx, "project").root == shared
    assert memory_tools.resolve_scope(ctx, "global").root == user_global.root

    block = _memory_index_block(ctx)
    assert "global-fact.md" in block
    assert "shared-chat-fact.md" in block
    assert "hidden-project-fact.md" not in block

    legacy = tmp_path / "legacy-memory"
    legacy_ctx = SimpleNamespace(
        deps=Deps(
            workspace=WorkspaceConfig(
                root=workspace,
                memory_root=legacy,
                project_memory_root=shared,
            )
        )
    )
    assert memory_tools.resolve_scope(legacy_ctx, "global").root == legacy / "global"
    assert memory_tools.resolve_scope(legacy_ctx, "project").root == legacy / "project"
