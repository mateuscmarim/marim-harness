import json
from collections.abc import Awaitable, Callable, Iterable

from pydantic_ai import RunContext

from ..jobs import JobRegistry, PrerequisiteFailed, _one_line
from ..runtime.deps import Deps
from ..workspace.agents import compose_subagent_task

# Default output budget for auto-detached spawns (≈3k tokens/report) — keeps a
# wide fan-out's synthesis prompt bounded while preserving the full report in the
# spill file. Only applied when the model did not pass an explicit max_output_chars.
_DETACH_OUTPUT_BUDGET = 12000


def _coerce_names(mcp: "list[str] | str | None") -> list[str] | None:
    """Normalize a name-list argument (mcp grant, after ids) into a list, or None.

    Weaker models often serialize the array argument as a JSON string
    (``'["mddocs"]'``) or a comma-separated string (``'mddocs, sentry'``)
    rather than a real array. Accepting those forms keeps a mis-encoding from
    failing the whole turn on schema validation. Returns None for an empty
    grant so it flows through as "no MCP access"."""
    if mcp is None:
        return None
    if isinstance(mcp, str):
        text = mcp.strip()
        if not text:
            return None
        # Deliberately NOT lenient._decode_json here: that helper returns the
        # *original string* on a parse failure, collapsing the very signal this
        # branch needs. A failed parse must route to comma-splitting below (the
        # common "mddocs, sentry" form), which the ``None`` sentinel + ``else``
        # branch encode; _decode_json's returned str would instead be caught by
        # the ``isinstance(parsed, str)`` branch and wrapped as a single name,
        # breaking comma-separated grants. The JSON *string* case (`'"mddocs"'`)
        # is the only overlap, so composing the helper would trade a correct
        # common case for a marginal one.
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            items: Iterable = parsed
        elif isinstance(parsed, str):
            items = [parsed]
        else:  # not JSON (or a number/object) — treat as comma-separated
            items = text.split(",")
    else:
        items = mcp
    names = [str(item).strip() for item in items]
    cleaned = [name for name in names if name]
    return cleaned or None


async def _run_after(
    jobs: "JobRegistry",
    after_ids: list[str],
    task: str,
    start_inner: "Callable[[str], Awaitable[str]]",
    state: dict,
) -> str:
    """Body of a dependent background job: wait for prerequisites, fail fast if
    any didn't succeed, then run the real sub-agent with their reports appended
    to its task.

    ``start_inner`` creates the inner run_background_agent coroutine *lazily* —
    the prompt can't be finalized until the prerequisites' reports exist, and an
    eagerly-created coroutine would leak un-awaited on a cancel-before-start
    (the same concern JobRegistry.register's docstring guards). ``state`` is
    shared with the job's output_fn so the jobs panel can show the waiting
    phase without a new job status."""
    settled = await jobs.await_settled(after_ids)
    bad = next((j for j in settled if j.status != "done"), None)
    if bad is not None:
        tail = " ".join((bad.result or "").split())[-160:]
        raise PrerequisiteFailed(
            f"prerequisite {bad.id} {bad.status}" + (f" — {tail}" if tail else "")
        )
    # Clip the heading to one line: a background spawn's label falls back to
    # the full composed (multi-section) task when `description` was omitted,
    # so without _one_line a dependent would receive its prerequisite's entire
    # prompt embedded inside its own "### job-N — ..." heading.
    sections = [
        f"### {j.id} — {_one_line(j.label)}\n{j.result or '(no output)'}" for j in settled
    ]
    full_task = task + "\n\n## Results of prerequisite jobs\n\n" + "\n\n".join(sections)
    state["waiting"] = False
    return await start_inner(full_task)


def _detach_handoff(job_id: str) -> str:
    """The return for an auto-detached spawn: tell the agent it's running in the
    background and that it may end its turn (wake will deliver the report) or wait."""
    return (
        f"Started detached sub-agent {job_id}, running in the background. "
        f"End your turn to let it run — its report will be "
        f"delivered to you when it finishes — or wait_for_job(\"{job_id}\") if you "
        f"need the result in this turn. For a fan-out, ending the turn is better."
    )


def _reject_spawn(
    ctx: "RunContext[Deps]",
    *,
    background: bool | None,
    auto_detached: bool,
    after_ids: list[str] | None,
) -> str | None:
    """Guard-fold for spawn_agent: the depth ceiling, the top-level-only
    background restriction, and the after= validations. Returns a refusal
    string to hand back to the model, or None to let the spawn proceed."""
    # Depth enforcement: refuse spawns that would exceed the depth ceiling. The
    # ceiling rides on Deps (SubagentRunner stamps its configured value into a
    # child's deps) rather than being a tool parameter — a parameter would sit
    # in the advertised schema, where the model could override it and raise its
    # own ceiling.
    effective_max = ctx.deps.subagent_max_depth
    if ctx.deps.subagent_depth + 1 >= effective_max:
        return (
            f"Cannot spawn sub-agent: already at depth "
            f"{ctx.deps.subagent_depth}, max depth is {effective_max}."
        )
    # Background spawning is main-agent-only: a sub-agent's turn ends before its
    # background child finishes, so the child's report would always be orphaned
    # (owned by the job registry, never seen by the spawner). Sub-agents should
    # fan out foreground children instead — results return to the caller.
    if background and ctx.deps.subagent_depth > 0:
        return (
            "Background spawning is only available to the top-level agent. "
            "Spawn this child in the foreground, or have the main agent "
            "spawn it as a background job with background=True."
        )
    if after_ids is not None:
        unknown = [jid for jid in after_ids if ctx.deps.jobs.get(jid) is None]
        if unknown:
            return (
                f"Cannot spawn with after={unknown}: no such job(s). "
                "after only accepts ids of already-started background jobs "
                "(see the jobs panel or the digest for valid ids)."
            )
        if not (background or auto_detached):
            return (
                "after= requires a detached spawn. Pass background=True (top-level "
                "agent only), or drop after and wait_for_job the prerequisite "
                "before a foreground spawn."
            )
    return None


async def _spawn_background(
    ctx: "RunContext[Deps]",
    *,
    type: str,
    task: str,
    description: str | None,
    mcp_names: list[str] | None,
    after_ids: list[str] | None,
    max_output_chars: int | None,
    auto_detached: bool,
    model: str | None,
    tier: str | None,
    thinking: str | None,
    isolation: str | None,
) -> str:
    """The background/auto-detached spawn path: registers a job — chained after
    `after_ids`'s prerequisites when given, else started immediately — and
    returns the handoff the model sees (a detach note when auto-detached, else
    a plain job-started line)."""
    if ctx.deps.services.run_background_agent is None:
        return "Background sub-agents are not available in this context."
    # For auto-detached spawns, default to _DETACH_OUTPUT_BUDGET when the
    # model did not pass an explicit cap — keeps the synthesis prompt bounded
    # across a wide fan-out while the full report is preserved in the spill file.
    if auto_detached and max_output_chars is None:
        budget = _DETACH_OUTPUT_BUDGET
    else:
        budget = max_output_chars
    # Prefer the short `description` for the job label (the jobs panel and the
    # wait row read it) — the composed `task` is a full multi-section prompt.
    label = f"{type}: {description or task}"
    if after_ids:
        state = {"waiting": True}
        waiting_note = f"(waiting on {', '.join(after_ids)})"
        # Type guard: we've already checked run_background_agent is not None
        # in the guard above, so this is safe.
        run_bg = ctx.deps.services.run_background_agent
        assert run_bg is not None

        def _waiting_output() -> str:
            return waiting_note if state["waiting"] else "(still running)"

        def _start_inner(full_task: str) -> "Awaitable[str]":
            return run_bg(
                type, full_task, mcp_names, budget, model, isolation,
                ctx.tool_call_id or "", ctx.deps.subagent_depth, tier, thinking,
            )

        job_id = ctx.deps.jobs.register(
            "agent", label,
            _run_after(ctx.deps.jobs, after_ids, task, _start_inner, state),
            output_fn=_waiting_output,
            stream_id=ctx.tool_call_id or None,
            prompt=task,
        )
    else:
        job_id = ctx.deps.jobs.register(
            "agent", label,
            ctx.deps.services.run_background_agent(
                type, task, mcp_names, budget, model, isolation,
                ctx.tool_call_id or "", ctx.deps.subagent_depth, tier, thinking,
            ),
            stream_id=ctx.tool_call_id or None,
            prompt=task,
        )
    if auto_detached:
        return _detach_handoff(job_id)
    return f"Started {job_id} (agent) — {label[:60]}"


async def spawn_agent(
    ctx: RunContext[Deps],
    type: str,
    task: str,
    description: str | None = None,
    background: bool | None = None,
    mcp: list[str] | str | None = None,
    after: "list[str] | str | None" = None,
    max_output_chars: int | None = None,
    returns: str | None = None,
    constraints: str | None = None,
    context: str | None = None,
    model: str | None = None,
    tier: str | None = None,
    thinking: str | None = None,
    isolation: str | None = None,
) -> str:
    """Delegate a sub-task to an isolated sub-agent that runs on the same model
    and reports back. `type` is a built-in — `explore` (read-only investigation;
    reports findings, changes nothing — use it to investigate before acting,
    especially over large files/logs/output you don't want cluttering your own
    context) or `general` (full toolset; carries out a focused sub-task
    autonomously) — or a custom agent by name, as listed in the sub-agents index.
    The sub-agent starts with a clean context, does `task`, and
    its final message becomes this tool's result. Spawn several in one turn to
    fan out independent work. Sub-agents can spawn deeper sub-agents, but are
    limited by a maximum nesting depth — attempts to spawn beyond that limit are
    refused.

    Leave `background` unset for a normal spawn or fan-out — that is almost always
    right. When detached-fanout mode is on, an unset spawn auto-detaches: it shows a
    live sub-agent card, returns a job handle, and you either end your turn (its
    report is delivered when it finishes) or wait_for_job for it inline. Only set
    `background=True` for a genuine fire-and-forget job; you do NOT need it to run a
    fan-out in parallel — unset already does, with better display. `background=False`
    forces an inline run (no detach).

    `mcp` grants the sub-agent specific MCP servers by name (none by default).
    Pass the names listed as enabled in the sub-agents index — e.g.
    `mcp=["mddocs"]` lets the sub-agent use that server's tools, gated the same
    way your own MCP calls are. Unknown or disabled names are ignored and noted
    in the report.

    `after` names background job ids (earlier detached spawns or bash jobs) that
    must finish before this spawn starts — use it to chain dependent work, e.g. a
    merge step after the jobs producing its inputs. It requires a detached spawn
    (`background=True`, or auto-detach). The prerequisites' final reports are
    appended to this sub-agent's task under "Results of prerequisite jobs"; size
    them with `max_output_chars` on the *prerequisite* spawns — injection never
    truncates. If a prerequisite fails or is cancelled, this job fails without
    starting (zero tokens spent) and the failure surfaces in the jobs digest.
    Prerequisite ids come from the spawn handoffs ("Started job-N …"); issue a
    dependent spawn in a later response, after those return — ids cannot be
    guessed.

    `max_output_chars` caps the report this spawn returns into your context — set
    it when you're fanning out and want bounded inflow. It's a budget the
    sub-agent distills toward (it's told to lead with the conclusion and
    summarize to fit), not a blind truncation. It's also enforced losslessly, for
    both foreground and background spawns: a report over budget is written to a
    workspace file and replaced with a within-budget head + a pointer to that
    file, so nothing is lost — you can read the file if you need the detail. Leave
    it unset for an unbounded report.

    `returns`, `constraints`, and `context` are optional structured fields folded
    into the sub-agent's prompt — all freeform text, all additive (omit any and
    nothing changes). Use them to give a clean-context sub-agent what it can't
    infer: `returns` is the output contract (what to hand back and in what shape —
    the highest-leverage field, since otherwise you get a shape you have to re-ask
    for); `constraints` are boundaries on how to work (a soft nudge — real tool
    reach is still set by `type`/`mcp`, not prose); `context` is the orchestration-
    level background it can't see (why this task, what's already known). The plain
    `task` stays the one required ask.

    `description` is an optional short (3-5 word) label for this spawn — it does
    not affect what the sub-agent does, only how the spawn is shown (it titles the
    sub-agent card and the tool line). Omit it and the card falls back to `task`.

    `tier` routes this spawn to one of your configured model tiers — `"cheap"`,
    `"med"`, or `"high"` — instead of your own model. Prefer this over `model`:
    pass `"cheap"` for read-only fan-out where a small model suffices, `"high"`
    for a hard sub-task. Omit it and the spawn takes its automatic tier (a
    read-only agent defaults to cheap, a workspace-mutating one to high; a custom
    agent may pin its own tier). A tier with no model configured falls back to
    your current model, so `tier` is always safe to pass.

    `model` is an advanced escape hatch: it names a specific model id to run this
    spawn on, bounded to your configured tier models. Prefer `tier` — reach for
    `model` only when you need an exact model the tiers don't cover. For a
    sub-agent whose definition sets `backend: claude-cli`, `model` is a Claude
    Code model name (e.g. `opus`, `sonnet`, a full id) passed straight to the
    CLI; `tier` does not apply to claude-cli spawns. Omit both to inherit your
    current model (the usual case).

    `thinking` overrides this spawn's reasoning effort — one of `"off"`,
    `"minimal"`, `"low"`, `"medium"`, `"high"`, `"xhigh"`. Omit it and the
    spawn inherits the sub-agent spec's own `thinking:` setting, then your
    session's level. `"off"` forces no reasoning effort for this spawn.

    `isolation="worktree"` runs a mutating spawn in its own git worktree, so
    several spawns editing files at once can't clobber each other or your working
    tree. Its changes are committed to a branch (named in the report) and the
    worktree is removed — merge or review the branch afterward. The worktree
    branches from the last commit, so it won't see uncommitted changes in your
    tree. Only needed when spawns write in parallel; omit for read-only work."""
    mcp_names = _coerce_names(mcp)
    after_ids = _coerce_names(after)
    if after_ids is not None:
        # Dedupe while preserving order: a model that lists the same
        # prerequisite id twice (e.g. after=[a, a]) would otherwise inject
        # that prerequisite's report twice into the dependent's task.
        after_ids = list(dict.fromkeys(after_ids))
    # Auto-detach (detached fan-out) is top-level-only, for the same reason the
    # explicit-background guard in _reject_spawn is: a sub-agent's turn ends
    # before a detached child finishes, so the child's report — owned by the
    # job registry — would never reach the spawner. A depth>0 spawn with
    # `background` unset runs inline instead. Computed before the guards
    # because they (and the background helper) both need it.
    auto_detached = (
        background is None
        and ctx.deps.subagent_depth == 0
        and ctx.deps.ui.detach_fanout
        and ctx.deps.ui.interactive
    )
    if r := _reject_spawn(
        ctx, background=background, auto_detached=auto_detached, after_ids=after_ids
    ):
        return r
    task = compose_subagent_task(
        task, returns=returns, constraints=constraints, context=context
    )
    if background or auto_detached:
        return await _spawn_background(
            ctx,
            type=type,
            task=task,
            description=description,
            mcp_names=mcp_names,
            after_ids=after_ids,
            max_output_chars=max_output_chars,
            auto_detached=auto_detached,
            model=model,
            tier=tier,
            thinking=thinking,
            isolation=isolation,
        )
    if ctx.deps.services.run_subagent is None:
        return "Sub-agents are not available in this context."
    # Pass the *caller's* depth so the runner builds the child at caller_depth + 1.
    # The runner can't read this off its own deps — those are fixed at the main
    # agent's depth (0), so a depth-1 sub-agent's spawn would otherwise be mis-sized.
    # The literal ``None`` before ``thinking`` is SubagentRunner.run's
    # ``output_schema`` slot (workflows-only; spawn_agent never sets a schema) —
    # it MUST stay explicit here, since ``thinking`` is a positional dispatch
    # and dropping this placeholder would silently shift ``thinking`` onto
    # ``output_schema`` instead of the runner's ``thinking`` param.
    return await ctx.deps.services.run_subagent(
        type, task, ctx.tool_call_id or "", mcp_names, max_output_chars, model,
        isolation, ctx.deps.subagent_depth, tier, None, thinking,
    )
