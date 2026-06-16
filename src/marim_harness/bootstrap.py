from pathlib import Path

from .agent import Harness, make_summarizer, make_titler
from .config import ModelSource, build_model, load_config
from .deps import Deps
from .permissions import Mode
from .session import SessionManager
from .tools.provider import BuiltinToolProvider

INSTRUCTIONS = (
    "You are a coding agent operating inside a workspace directory. "
    "Use the provided tools to read, search, and edit files and run commands. "
    "Always read a file before editing it. Keep changes minimal and focused."
)


def build_harness(
    workspace: Path,
    *,
    mode: Mode,
    resume: bool = False,
) -> Harness:
    """Construct a ready-to-run Harness for ``workspace``. Shared by the TUI and
    the headless CLI so both wire up the model, session store, and aux agents
    identically. When ``resume`` is set, reattaches to the latest saved session
    and replays its history."""
    cfg = load_config()
    model = build_model(cfg)
    deps = Deps(workspace_root=workspace, mode=mode)

    manager = SessionManager(workspace)
    latest = manager.latest() if resume else None
    store = manager.store(latest.id) if latest is not None else manager.create()

    harness = Harness(
        model=model,
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions=INSTRUCTIONS,
        model_label=f"{cfg.provider}/{cfg.model}",
        store=store,
        manager=manager,
        max_context_tokens=cfg.max_context_tokens,
        summarizer=make_summarizer(model),
        titler=make_titler(model),
        model_source=ModelSource(cfg),
        model_id=cfg.model,
        proactive_memory=cfg.proactive_memory,
    )
    if resume:
        harness.resume()
    return harness
