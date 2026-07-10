"""Embed marim-harness as a tiny architecture-decision assistant.

A worked example of the SDK surface (`docs/embedding.md`, `docs/sdk/`): build a
`Harness` with `HarnessBuilder`, register a custom **gated** tool and a
read-only one, and drive real turns — the same approval loop, tool wiring, and
session behavior the CLI gets, composed explicitly with no `MARIM_*` env reads.

Run it live against your own key (pydantic-ai reads the provider's env var):

    ANTHROPIC_API_KEY=... uv run python examples/embedding/assistant.py \
        "Record that we picked SQLite for the cache — it's zero-ops."

Or drive it network-free from a test — see `tests/test_examples_embedding.py`,
which passes a scripted `FunctionModel` through the `model=` seam below.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

# RunContext and Deps MUST be real runtime imports — never under a
# `TYPE_CHECKING` guard. Under `from __future__ import annotations` every
# annotation is a string, and pydantic-ai resolves a tool's annotations with
# `get_type_hints()` against this module's globals when `build()` registers it.
# A type-only import leaves `Deps` unresolvable there and the build fails. See
# docs/sdk/custom-tools.md.
from pydantic_ai import RunContext

from marim_harness import Deps, HarnessBuilder, Mode

DEFAULT_MODEL = "anthropic:claude-sonnet-4-6"  # any pydantic-ai model string
DECISIONS_FILE = "DECISIONS.md"


def make_record_decision_tool(rel_path: str):
    """Build the gated ``record_decision`` tool, bound to ``rel_path``.

    A factory closure captures run-scoped config (the log's path) without a
    module global — the returned function is what you hand to ``with_tool``.
    This mirrors the ``make_*_tool`` pattern in docs/sdk/custom-tools.md.
    """

    def record_decision(ctx: RunContext[Deps], title: str, rationale: str) -> str:
        """Record an architecture decision in the project's decision log.

        `title` is a short imperative summary (e.g. "Use SQLite for the cache").
        `rationale` is one or two sentences on why. Call this once the user has
        settled on a decision worth remembering; check `list_decisions` first to
        avoid logging a duplicate.
        """
        title, rationale = title.strip(), rationale.strip()
        if not title:
            return "error: title is empty"
        if not rationale:
            return "error: rationale is empty"
        # Tools return plain strings — including validation errors, which the
        # model reads and can recover from — never raise for bad model input.
        path = ctx.deps.workspace.root / rel_path
        with path.open("a", encoding="utf-8") as f:
            f.write(f"## {title}\n\n{rationale}\n\n")
        return f"recorded decision {title!r} in {rel_path}"

    return record_decision


def list_decisions(ctx: RunContext[Deps]) -> str:
    """List the titles already in the project's decision log.

    Read-only, so it is registered ungated — it runs without an approval round.
    """
    path = ctx.deps.workspace.root / DECISIONS_FILE
    if not path.exists():
        return "no decisions recorded yet"
    titles = [
        line[3:].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]
    return "\n".join(f"- {t}" for t in titles) if titles else "no decisions recorded yet"


def build_assistant(workspace: Path, *, model=None):
    """Compose the assistant `Harness`.

    ``model=None`` uses the production default; tests and local smoke runs pass
    a pydantic-ai `FunctionModel`/`TestModel` through this same seam so no
    network or API key is needed (docs/sdk/testing.md).
    """
    return (
        HarnessBuilder(workspace=workspace, model=model or DEFAULT_MODEL)
        # Mode.auto is already the builder default; shown here to make the
        # choice explicit. Use Mode.ask to gate writes behind a human, or
        # Mode.plan for a read-only research turn.
        .with_mode(Mode.auto)
        .with_tool(list_decisions)  # read-only → ungated
        .with_tool(  # mutates the workspace → gated
            make_record_decision_tool(DECISIONS_FILE), requires_approval=True
        )
        .build()
    )


async def run_agent_turn(prompt: str, workspace: Path, *, model=None) -> str:
    """The one place the CLI touches the harness: build, run a turn, return the reply.

    Keeping this a module-level function (not inlined in ``main``) is the seam a
    test drives with a scripted model.
    """
    harness = build_assistant(workspace, model=model)
    return await harness.run_turn(prompt)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="marim embedding example: an architecture-decision assistant."
    )
    parser.add_argument("prompt", help="what to ask the assistant")
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        type=Path,
        help="workspace directory the assistant reads/writes (default: current dir)",
    )
    args = parser.parse_args()
    print(asyncio.run(run_agent_turn(args.prompt, args.workspace)))


if __name__ == "__main__":
    main()
