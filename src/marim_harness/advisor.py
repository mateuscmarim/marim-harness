"""The advisor: a separately-configured, typically stronger model the main
agent can consult mid-task through the ``advisor`` tool.

Client-side replica of Anthropic's advisor tool (theirs is the server-side
``advisor_20260301``; marim runs on arbitrary providers, so the consult is a
plain tool-free one-shot run here). ``make_advisor`` mirrors
``compaction.make_summarizer``: a dedicated tool-free agent reads the rendered
transcript and returns guidance text. Which model it consults is re-resolved
PER CALL through ``get_model_id`` — that per-call resolution is what makes a
mid-session ``/advisor`` switch live without an agent rebuild.

Every failure path returns a short actionable STRING, never raises: the advice
lands in a tool result, and a broken advisor must degrade the advice, not the
turn (see the design spec's error-handling section).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

from .compaction import render_transcript
from .session.ctrl import aux_model_for

if TYPE_CHECKING:
    from pydantic_ai.models import Model

# (messages) -> advice text. The advisor tool passes the in-flight run history
# (ctx.messages); errors come back as text.
AdviseFn = Callable[[list], Awaitable[str]]

# Session-persistence sentinel: SessionStore.advisor_model == "off" means the
# user explicitly disabled the advisor for that session, which must survive
# restarts distinguishably from None ("unset — inherit the env default").
ADVISOR_OFF = "off"

_ADVISOR_INSTRUCTIONS = (
    "You are a senior engineer advising a coding agent mid-task. You will be "
    "shown the full transcript of its session so far: the user's request, the "
    "agent's reasoning, and every tool call and result. Give focused strategic "
    "guidance: whether the current approach is sound, risks or mistakes you "
    "see, and what to check or do before proceeding. Be concise and concrete. "
    "Do not restate the transcript, do not write the code yourself, and do not "
    "address the user — you are speaking to the agent."
)

# Appended to the main agent's system prompt (see runtime/instructions.py) only
# while an advisor is configured — the same ``services.advise`` seam gates the
# tool itself, so prompt and tool availability cannot drift. Soft steering only:
# Anthropic's timing + weigh-the-advice blocks, no hard-rule enforcement.
ADVISOR_GUIDANCE = (
    "An advisor tool is available: calling it sends the full conversation "
    "transcript to a stronger reviewer model and returns strategic guidance.\n"
    "When to consult the advisor:\n"
    "- Before starting substantive work on a non-trivial task, once you have "
    "gathered the relevant context.\n"
    "- When you are stuck, going in circles, or about to make a risky or "
    "hard-to-reverse change.\n"
    "- Before declaring a complex task done, to check for gaps.\n"
    "Skip it for trivial questions and simple mechanical edits.\n"
    "Weighing the advice: the advisor sees only the transcript, not the live "
    "workspace. Treat its guidance as a strong signal, not an order — when it "
    "conflicts with direct evidence you gathered from files or command "
    "output, trust your evidence and say why."
)

# First attempt renders the transcript at render_transcript's default clip;
# the retry tightens it hard, on the theory that the most likely run failure
# is a context overflow on the advisor's (unknown) window. One retry only —
# a second failure surfaces as the error string.
_CLIP_ATTEMPTS = (2000, 400)


def _advise_prompt(transcript: str) -> str:
    """Wrap the transcript in an explicit, in-message advice instruction. As
    with compaction's ``_summarize_prompt``: rules only in the system prompt
    let weaker models reply conversationally, and under a claude-cli advisor
    our instructions are merely appended to Claude Code's own prompt — so the
    task is restated in the user turn."""
    return (
        "You are advising the coding agent whose session transcript follows. "
        "Following the rules in your instructions, give focused strategic "
        "guidance: approach soundness, risks, and what to check before "
        "proceeding. Output only the advice — do not restate the transcript "
        "or address the user.\n\n"
        "=== TRANSCRIPT START ===\n"
        f"{transcript}\n"
        "=== TRANSCRIPT END ===\n\n"
        "Advice:"
    )


def make_advisor(
    build_model: Callable[[str], Model],
    get_model_id: Callable[[], str | None],
    *,
    cwd: str,
    max_tokens: int = 2048,
) -> AdviseFn:
    """Build the advice callable bound to ``services.advise``.

    ``build_model`` turns a model id into a Model (the Harness supplies
    ``MultiModelSource.build`` when a source exists, else ``infer_model``).
    ``get_model_id`` is a live getter (closing over the Harness's mutable
    advisor id) so ``/advisor`` switches apply to the next consultation.
    ``cwd`` feeds ``aux_model_for``'s claude-cli ephemeral clone."""

    async def advise(messages: list) -> str:
        model_id = get_model_id()
        if not model_id:
            return (
                "Advisor unavailable: no advisor model is configured. "
                "Continue without advice."
            )
        try:
            # A claude-cli advisor must not share the session-carrying CLI
            # instance — aux_model_for swaps in a stateless ephemeral clone,
            # the same guard the summarizer/titler get.
            model = aux_model_for(build_model(model_id), cwd=cwd)
        except Exception as exc:
            return (
                f"Advisor unavailable: can't build model {model_id!r}: {exc}. "
                "Continue without advice."
            )
        agent = Agent(
            model,
            instructions=_ADVISOR_INSTRUCTIONS,
            model_settings=ModelSettings(max_tokens=max_tokens),
        )
        last_error: Exception | None = None
        for clip in _CLIP_ATTEMPTS:
            try:
                result = await agent.run(
                    _advise_prompt(render_transcript(messages, max_part_chars=clip))
                )
            except Exception as exc:
                last_error = exc
                continue
            usage = result.usage
            return (
                f"{result.output}\n\n"
                f"[advisor usage: {usage.input_tokens or 0} in, "
                f"{usage.output_tokens or 0} out tokens]"
            )
        return f"Advisor unavailable: {last_error}. Continue without advice."

    return advise
