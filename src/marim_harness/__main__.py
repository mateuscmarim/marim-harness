import argparse
from pathlib import Path

from .agent import Harness, make_summarizer, make_titler
from .config import ModelSource, build_model, load_config, load_environment
from .deps import Deps
from .permissions import Mode
from .session import SessionManager
from .tools.provider import BuiltinToolProvider
from .tui.app import HarnessApp

_INSTRUCTIONS = (
    "You are a coding agent operating inside a workspace directory. "
    "Use the provided tools to read, search, and edit files and run commands. "
    "Always read a file before editing it. Keep changes minimal and focused."
)


def main() -> None:
    # Load the project-local .env (cwd/parents), falling back to the global
    # config (~/.config/marim/.env) so `marim` works from any directory. Real
    # environment variables still take priority.
    load_environment()
    parser = argparse.ArgumentParser(prog="marim")
    parser.add_argument(
        "workspace", nargs="?", default=None,
        help="workspace directory (defaults to the current directory)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="resume the saved conversation for this workspace",
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
    cfg = load_config()
    model = build_model(cfg)
    deps = Deps(workspace_root=workspace, mode=Mode.ask)

    manager = SessionManager(workspace)
    latest = manager.latest() if args.resume else None
    store = manager.store(latest.id) if latest is not None else manager.create()

    harness = Harness(
        model=model,
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions=_INSTRUCTIONS,
        model_label=f"{cfg.provider}/{cfg.model}",
        store=store,
        manager=manager,
        max_context_tokens=cfg.max_context_tokens,
        summarizer=make_summarizer(model),
        titler=make_titler(model),
        model_source=ModelSource(cfg),
        model_id=cfg.model,
    )
    if args.resume:
        harness.resume()
    HarnessApp(harness).run()


if __name__ == "__main__":
    main()
