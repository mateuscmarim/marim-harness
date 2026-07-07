import logging
from datetime import datetime, timezone

from pydantic_ai import ModelRetry, RunContext

from ..ask_user import Choice, Question, answers_to_json, coerce_questions
from ..runtime.deps import CurrentPlan, Deps
from ..runtime.permissions import Mode
from ..tasks import Task, summarize
from ..workspace.plans import write_plan
from .lenient import Lenient, LenientList

logger = logging.getLogger(__name__)

_ASK_USER_EMPTY = "ask_user needs at least one question, each with at least one option."
_ASK_USER_NO_UI = (
    "Can't ask the user — no interactive UI here. Proceed with your best judgment."
)
_ASK_USER_CANCELLED = "User dismissed the prompt without answering."


async def update_tasks(ctx: RunContext[Deps], todos: LenientList[Lenient[Task]]) -> str:
    """Maintain your checklist for the current multi-step task. Pass the
    FULL list every time — it replaces the previous one. Each item is
    {text, status} where status is pending, in_progress, or done. Keep
    exactly one item in_progress, and mark items done as you finish them.
    Use this for non-trivial work spanning several steps so progress is
    visible; skip it for single-step requests. No approval is needed."""
    before = {t.text: t.status for t in ctx.deps.tasks.items}
    ctx.deps.tasks.replace(todos)
    th = ctx.deps.services.turn_hooks
    if th is not None:
        for t in ctx.deps.tasks.items:
            if t.status == "done" and before.get(t.text) != "done":
                await th.task_completed(task_subject=t.text)
    return summarize(ctx.deps.tasks.items)


async def ask_user(ctx: RunContext[Deps], questions: LenientList[Lenient[Question]]) -> str:
    """Ask the user to choose between concrete options, pausing your turn until
    they answer. Use this only when the user's decision changes what you do next
    and you can't settle it yourself or from the code — not for things you can
    verify or reasonably assume.

    Pass 1–4 questions. Each is {question, header, options, multi}: `header` is a
    short label the answer is returned under; `options` is a list of {label,
    description} choices (description optional); set `multi` true to let the user
    pick several. A free-text field is offered on every question automatically —
    don't add an "other" option yourself.

    Returns a JSON object keyed by each question's `header`: a single-select
    answer is the chosen label (or the user's typed free text); a multi-select
    answer is a list of chosen labels. If there's no interactive UI, or the user
    dismisses the prompt, you get a short note instead — proceed with your best
    judgment."""
    coerced = coerce_questions(questions)
    if not coerced:
        return _ASK_USER_EMPTY
    if ctx.deps.ui.ask_user is None:
        return _ASK_USER_NO_UI
    th = ctx.deps.services.turn_hooks
    if th is not None:
        await th.notification(
            "ask_user", "Question from agent", coerced[0].question
        )
    answers = await ctx.deps.ui.ask_user(coerced)
    if not answers:
        return _ASK_USER_CANCELLED
    return answers_to_json(answers)


_PLAN_CHOICES = [
    Choice("Execute hands-off (auto)", "Run the whole plan without further prompts."),
    Choice("Execute step-by-step (ask)", "Run the plan, approving each change."),
    Choice("Hand off to sub-agent", "Spawn a sub-agent to implement the plan file."),
    Choice("Keep planning", "Save the plan as a draft and keep refining."),
]
_PLAN_EXEC_MODES = {
    "Execute hands-off (auto)": Mode.auto,
    "Execute step-by-step (ask)": Mode.ask,
}


async def present_plan(
    ctx: RunContext[Deps], summary: str, steps: LenientList[str]
) -> str:
    """Present your finished plan and let the user choose how to execute it. Call
    this at the END of a planning turn, once you have researched the task and have
    a concrete, ordered plan.

    `summary` is a short paragraph describing the approach; `steps` is the ordered
    list of concrete steps. The plan is saved to `.marim/plans/`, mirrored into
    your task checklist, and the user is asked whether to execute it hands-off,
    step-by-step, hand it to a sub-agent, or keep planning. If they approve
    execution, the approval mode switches and you should begin carrying out the
    plan starting at step one. If there is no interactive UI, the plan is saved
    and you stay in plan mode."""
    clean = [s.strip() for s in (steps or []) if s and s.strip()]
    if not clean:
        raise ModelRetry("present_plan needs at least one concrete step.")

    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Stamp the plan with the active session id so filenames are stable per session
    # (one file per plan per session, re-presenting overwrites). The getter is wired
    # by the Harness; it is None in headless/tests, where we fall back to the
    # workspace root name so the slug is still stable and non-empty.
    get_sid = ctx.deps.services.get_session_id
    sid = get_sid() if get_sid is not None else None
    session_id = sid or ctx.deps.workspace.root.name or "session"
    try:
        path = write_plan(
            ctx.deps.workspace.root,
            session_id=session_id,
            summary=summary,
            steps=clean,
            created=created,
        )
    except OSError:
        logger.warning("failed to write plan artifact", exc_info=True)
        path = None

    ctx.deps.tasks.replace([Task(text=s) for s in clean])
    # Record the plan narrative so the pinned TaskPanel title and the PlanScreen
    # overlay can show the "why" after the transient PlanCard scrolls away. Step
    # progress stays in ctx.deps.tasks (set just above) — the single source of
    # truth — so this holds only summary/steps/path.
    ctx.deps.plan = CurrentPlan(summary=summary, steps=clean, path=str(path) if path else None)

    # Prefer the dedicated plan card (on_present_plan) when a UI is attached; it
    # renders the plan + choices as one deliberate inline moment. Fall back to the
    # generic ask_user panel if only that is wired, then to the headless path.
    if ctx.deps.ui.on_present_plan is not None:
        decision = await ctx.deps.ui.on_present_plan(summary, clean, _PLAN_CHOICES)
        choice, feedback = decision.choice, decision.feedback
    elif ctx.deps.ui.ask_user is not None:
        answers = await ctx.deps.ui.ask_user(
            [Question(question="How should I execute this plan?", header="execution",
                      options=_PLAN_CHOICES)]
        )
        choice = (answers or {}).get("execution", "Keep planning")
        # A free-text answer (not one of the known choice labels) is revise-feedback,
        # mirroring the PlanCard's feedback field.
        known = {c.label for c in _PLAN_CHOICES}
        if isinstance(choice, str) and choice not in known:
            feedback = choice
            choice = "Keep planning"
        else:
            feedback = None
    else:
        return (
            f"Plan saved{f' to {path}' if path else ''}. No interactive UI, so "
            "staying in plan mode — share the plan and await direction."
        )

    new_mode = _PLAN_EXEC_MODES.get(choice if isinstance(choice, str) else "")
    if new_mode is not None:
        # Tools hold only ctx.deps (not the Harness), so set mode directly; the
        # on_mode_change hook below performs the UI refresh that Harness.set_mode
        # would otherwise trigger.
        ctx.deps.workspace.mode = new_mode
        if ctx.deps.ui.on_mode_change is not None:
            ctx.deps.ui.on_mode_change()
        return (
            f"Plan approved. Approval mode is now {new_mode.value}. Begin executing "
            "the plan now, starting with step one."
        )
    if choice == "Hand off to sub-agent" and path is not None:
        return (
            f"Plan saved to {path}. To execute, call spawn_agent (type 'general') "
            f"with instructions to implement the steps in {path} in order, then "
            "report back. You remain in plan mode meanwhile."
        )
    if feedback:
        return (
            "Plan not approved. The user wants you to revise it. Their feedback: "
            f"{feedback}\n\nRevise the plan accordingly and call present_plan again "
            "when ready."
        )
    return (
        f"Plan saved{f' to {path}' if path else ''} as a draft. Still in plan mode "
        "— refine it and call present_plan again when ready."
    )
