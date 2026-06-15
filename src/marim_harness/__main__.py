import sys
from pathlib import Path

from dotenv import load_dotenv

from .agent import Harness
from .config import build_model, load_config
from .deps import Deps
from .permissions import Mode
from .tools.provider import BuiltinToolProvider
from .tui.app import HarnessApp

_INSTRUCTIONS = (
    "You are a coding agent operating inside a workspace directory. "
    "Use the provided tools to read, search, and edit files and run commands. "
    "Always read a file before editing it. Keep changes minimal and focused."
)


def main() -> None:
    # Load .env from the current directory (or a parent) so keys can live in a
    # file instead of the shell. Real environment variables still take priority.
    load_dotenv()
    workspace = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    cfg = load_config()
    model = build_model(cfg)
    deps = Deps(workspace_root=workspace, mode=Mode.ask)
    harness = Harness(
        model=model,
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions=_INSTRUCTIONS,
        model_label=f"{cfg.provider}/{cfg.model}",
    )
    HarnessApp(harness).run()


if __name__ == "__main__":
    main()
