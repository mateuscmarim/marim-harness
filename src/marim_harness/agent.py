from contextlib import AsyncExitStack
from typing import Awaitable, Callable, Optional

from pydantic_ai import Agent, DeferredToolRequests, RunContext
from pydantic_ai.usage import RunUsage

from .agents import (
    agents_index_text,
    discover_agents,
    effective_tools,
    find_agent,
    subagent_instructions,
)
from .compaction import (
    Summarizer,
    compact_history,
    compact_history_with_summary,
    render_transcript,
)
from .deps import Deps
from .instructions import load_project_instructions
from .mcp import persist_server_enabled
from .memory import global_scope, load_index, project_scope
from .permissions import Mode, resolve_approvals
from .session import SessionInfo, SessionManager, SessionStore
from .skills import discover_skills, skills_index_text
from .tasks import render_tasks
from .tools.provider import ToolProvider

_SUMMARY_INSTRUCTIONS = (
    "You compress a coding-session transcript into a dense summary so the agent "
    "can keep working with less context. Preserve: the user's goals and "
    "constraints, decisions made, files read or edited and what changed, command "
    "results, and any unresolved problems or next steps. Drop pleasantries and "
    "redundant detail. Write terse notes, not prose."
)


def make_summarizer(model) -> Summarizer:
    """Build a summarizer backed by a dedicated, tool-free agent on ``model``."""
    summary_agent = Agent(model, instructions=_SUMMARY_INSTRUCTIONS)

    async def summarize(messages: list) -> str:
        result = await summary_agent.run(render_transcript(messages))
        return result.output

    return summarize


Titler = Callable[[list], Awaitable[str]]

_TITLE_INSTRUCTIONS = (
    "You write a short, specific title for a coding session from its transcript. "
    "Reply with the title only — no quotes, no trailing punctuation, at most six "
    "words. Name the concrete task, e.g. 'Fix the parser off-by-one' or 'Add "
    "session auto-naming'."
)

_MAX_TITLE_CHARS = 50


def clean_title(raw: str) -> str:
    """Reduce a model's reply to a single tidy title line, with a safe fallback."""
    lines = [line.strip() for line in (raw or "").splitlines()]
    text = next((line for line in lines if line), "")
    if text.lower().startswith("title:"):
        text = text[len("title:"):].strip()
    text = text.strip("\"'`").strip().rstrip(".!?,;:").strip()
    if len(text) > _MAX_TITLE_CHARS:
        text = text[:_MAX_TITLE_CHARS].rstrip() + "…"
    return text or "Untitled session"


def make_titler(model) -> Titler:
    """Build a titler backed by a dedicated, tool-free agent on ``model``."""
    title_agent = Agent(model, instructions=_TITLE_INSTRUCTIONS)

    async def title(messages: list) -> str:
        result = await title_agent.run(render_transcript(messages))
        return clean_title(result.output)

    return title


_PROACTIVE_MEMORY_POLICY = (
    "Proactive memory is ON. Beyond explicit requests, save durable facts that "
    "will help in future sessions with the remember tool: the user's stable "
    "preferences and identity, feedback they give on how you should work, and "
    "project conventions or decisions not derivable from the code or git "
    "history. Convert relative dates to absolute. Do NOT save anything "
    "recoverable from the code, files, or git; one-off conversational details; "
    "or secrets. Prefer updating an existing memory over adding a duplicate."
)

_ON_REQUEST_MEMORY_POLICY = (
    "Save to memory only when the user explicitly asks you to (for example, "
    "\"remember that …\" or the /remember command). Do not save memories "
    "proactively or on your own initiative, even if the user mentions a "
    "preference or fact in passing."
)


class Harness:
    """Owns the Pydantic AI agent and drives one user turn to completion,
    resolving deferred tool approvals by the current mode."""

    def __init__(self, model, provider: ToolProvider, deps: Deps, instructions: str,
                 model_label: str = "model", store: Optional[SessionStore] = None,
                 manager: Optional[SessionManager] = None,
                 max_context_tokens: int = 100_000, keep_last_messages: int = 20,
                 summarizer: Optional[Summarizer] = None,
                 titler: Optional[Titler] = None, model_source=None,
                 model_id: Optional[str] = None, proactive_memory: bool = False,
                 mcp_servers=None, mcp_disabled=None):
        self.proactive_memory = proactive_memory
        self.agent = Agent(
            model,
            deps_type=Deps,
            instructions=instructions,
            output_type=[str, DeferredToolRequests],
        )
        self.provider = provider
        provider.register(self.agent)

        @self.agent.instructions
        def _project_instructions(ctx: RunContext[Deps]) -> str:
            """Layer the workspace's AGENTS.md on top of the base prompt, re-read
            each turn so edits take effect without a restart."""
            text = load_project_instructions(ctx.deps.workspace_root)
            if not text:
                return ""
            return f"Project-specific instructions from AGENTS.md:\n\n{text}"

        @self.agent.instructions
        def _memory_indexes(ctx: RunContext[Deps]) -> str:
            """Inject the global and project memory indexes, re-read each turn.
            Each line names a memory the model can expand with the recall tool;
            new facts are saved with the remember tool."""
            parts = []
            g = load_index(global_scope())
            if g:
                parts.append(f"# User memory (global)\n\n{g}")
            p = load_index(project_scope(ctx.deps.workspace_root))
            if p:
                parts.append(f"# Project memory\n\n{p}")
            if not parts:
                return ""
            return (
                "Persistent memory indexes below. Each line is a one-line hook; "
                "read the full fact with the recall tool (by the entry's title or "
                "slug, with the matching scope). Save new durable facts with the "
                "remember tool.\n\n" + "\n\n".join(parts)
            )

        @self.agent.instructions
        def _skill_index(ctx: RunContext[Deps]) -> str:
            """Inject the discovery index of available skills, re-read each turn.
            Each line is a packaged workflow the model can pull in with the
            activate_skill tool; manual-only skills are omitted by the formatter."""
            text = skills_index_text(discover_skills(ctx.deps.workspace_root))
            if not text:
                return ""
            return (
                "Available skills below — each is a packaged workflow. When a "
                "task matches one's description, load its full instructions with "
                "the activate_skill tool (by name) and follow them.\n\n" + text
            )

        @self.agent.instructions
        def _agent_index(ctx: RunContext[Deps]) -> str:
            """List the sub-agents the model can delegate to with spawn_agent,
            re-read each turn so a newly-added custom agent shows up immediately.
            Always present — the built-in explore/general are always available."""
            text = agents_index_text(discover_agents(ctx.deps.workspace_root))
            return (
                "Sub-agents you can delegate to with the spawn_agent tool (each "
                "runs in isolation and reports back; spawn several in one turn to "
                "fan out independent work):\n\n" + text
            )

        @self.agent.instructions
        def _mcp_index(ctx: RunContext[Deps]) -> str:
            """Name the MCP servers a spawn may grant, re-read each turn so a
            server toggled on/off mid-session is reflected. Silent when none are
            enabled."""
            return self.mcp_index_text()

        @self.agent.instructions
        def _task_state(ctx: RunContext[Deps]) -> str:
            """Inject the current checklist each turn so the model continues from
            the live state, and remind it how to keep the list current. Silent
            when there are no tasks — the update_tasks tool docstring covers when
            to start one."""
            items = ctx.deps.tasks.items
            if not items:
                return ""
            return (
                "Your current task checklist (✔ done · ▸ in progress · ○ "
                "pending):\n\n" + render_tasks(items) + "\n\nKeep it current with "
                "the update_tasks tool: pass the full list, keep one item in "
                "progress, and mark items done as you complete them."
            )

        @self.agent.instructions
        def _memory_policy(ctx: RunContext[Deps]) -> str:
            """Standing memory policy. The remember tool is always available, so
            the default must actively restrain proactive saves; the toggle flips
            that restraint into encouragement."""
            if self.proactive_memory:
                return _PROACTIVE_MEMORY_POLICY
            return _ON_REQUEST_MEMORY_POLICY

        self.deps = deps
        # The spawn_agent tool reaches the runners through Deps, the same way
        # other tools reach shared state. Wired here so they track model switches.
        self.deps.run_subagent = self._run_subagent
        self.deps.run_background_agent = self._run_background_subagent
        self.history: list = []
        self.model_label = model_label
        # The model object used for each turn (swappable at runtime), the source
        # that builds new ones, and the id of the active model.
        self.current_model = model
        self.model_source = model_source
        self.model_id = model_id
        self.usage = RunUsage()
        self.store = store
        self.manager = manager
        self.max_context_tokens = max_context_tokens
        self.keep_last_messages = keep_last_messages
        self.summarizer = summarizer
        self.titler = titler
        # Called with (messages_before, messages_after) when history is compacted.
        self.on_compact: Optional[Callable[[int, int], None]] = None
        # Called with (old_name, new_name) when a session is auto-titled.
        self.on_rename: Optional[Callable[[str, str], None]] = None
        # Configured MCP servers and the subset whose connections are live. Each
        # run additively passes the live servers as toolsets; connections are
        # refcounted, so entering once here keeps them up across runs.
        self.mcp_servers: list = list(mcp_servers or [])
        self._live_servers: list = []
        self._mcp_stack: Optional[AsyncExitStack] = None
        self._connected = False
        # Names turned off — seeded from the config's ``enabled: false`` and then
        # toggled at runtime by /mcp enable|disable. A disabled server is never
        # launched by connect(); a server disabled while live stays connected but
        # its tools stop being offered (see run_turn). Servers stay built either
        # way, so a config-disabled one can still be enabled in-session.
        self.disabled: set[str] = set(mcp_disabled or [])
        # Outcome of the last connect(): {"connected": [names], "failed":
        # [(name, error)]}. Read by the /mcp command to report status on demand.
        self.mcp_status: dict = {"connected": [], "failed": []}

    @property
    def total_tokens(self) -> int:
        """Cumulative input + output tokens across the whole session."""
        return self.usage.total_tokens

    def set_model(self, model_id: str, *, persist: bool = True) -> None:
        """Switch the active model at runtime. Rebuilds the per-turn model and
        any configured aux agents (summarizer/titler) on the new model, updates
        the label, and records the choice on the session. No-op without a source.
        """
        if self.model_source is None:
            return
        model = self.model_source.build(model_id)
        self.current_model = model
        self.model_id = model_id
        self.model_label = self.model_source.label(model_id)
        if self.summarizer is not None:
            self.summarizer = make_summarizer(model)
        if self.titler is not None:
            self.titler = make_titler(model)
        if self.store is not None:
            self.store.model = model_id
            if persist:
                self._persist()

    def _apply_saved_model(self) -> None:
        """Re-point at a session's saved model after loading it, if one differs
        from what's already active."""
        if (
            self.store is not None
            and self.store.model
            and self.model_source is not None
            and self.store.model != self.model_id
        ):
            self.set_model(self.store.model, persist=False)

    def resume(self) -> int:
        """Load a previously saved conversation for this workspace into history.
        Returns the number of messages restored (0 if none / no store)."""
        if self.store is None:
            return 0
        self.history, self.usage, tasks = self.store.load()
        self.deps.tasks.load(tasks)
        self._apply_saved_model()
        return len(self.history)

    def reset(self) -> None:
        """Drop the conversation: clear history, token counters, tasks, and any
        saved session for this workspace. Used by the /clear command."""
        self.history = []
        self.usage = RunUsage()
        self.deps.tasks.clear()
        if self.store is not None:
            self.store.clear()

    @property
    def session_name(self) -> Optional[str]:
        """Display name of the active session, if any."""
        return self.store.name if self.store is not None else None

    def sessions(self) -> list[SessionInfo]:
        """All saved sessions for this workspace, newest first."""
        if self.manager is None:
            return []
        return self.manager.list()

    def new_session(self, name: Optional[str] = None) -> None:
        """Start a fresh session (new store), leaving existing ones untouched."""
        if self.manager is None:
            self.reset()
            return
        self.store = self.manager.create(name)
        self.store.model = self.model_id  # keep the current model on the new session
        self.history = []
        self.usage = RunUsage()
        self.deps.tasks.clear()

    def switch_session(self, session_id: str) -> int:
        """Switch to an existing session, loading its history. Returns the number
        of messages restored."""
        if self.manager is None:
            return 0
        self.store = self.manager.store(session_id)
        self.history, self.usage, tasks = self.store.load()
        self.deps.tasks.load(tasks)
        self._apply_saved_model()
        return len(self.history)

    def _persist(self) -> None:
        if self.store is not None:
            self.store.save(self.history, self.usage, self.deps.tasks.to_payload())

    async def _maybe_autoname(self) -> None:
        """After a turn, give an unnamed session an LLM-generated title (once).
        Silent on failure — a titling hiccup must never break the turn."""
        if (
            self.titler is None
            or self.store is None
            or not self.store.auto_named
            or not self.history
        ):
            return
        old = self.store.name
        try:
            title = await self.titler(self.history)
        except Exception:
            return
        if not title:
            return
        self.store.name = title
        self.store.auto_named = False
        self._persist()
        if self.on_rename is not None:
            self.on_rename(old, title)

    async def rename_session(self, name: Optional[str] = None) -> Optional[str]:
        """Rename the active session. With ``name``, set it verbatim; without,
        generate one from the conversation via the titler. Returns the new name,
        or None if it couldn't be done (no store, no titler, empty conversation).
        """
        if self.store is None:
            return None
        if name:
            new = name.strip()
        elif self.titler is not None and self.history:
            try:
                new = await self.titler(self.history)
            except Exception:
                return None
        else:
            return None
        if not new:
            return None
        self.store.name = new
        self.store.auto_named = False
        self._persist()
        return new

    async def _maybe_compact(self) -> None:
        """Compact history if it has grown past the token budget, keeping the task
        anchor and a recent tail. Summarizes the dropped middle when a summarizer
        is configured (falling back to truncation); fires on_compact when it trims.
        """
        before = len(self.history)
        if self.summarizer is not None:
            new_history, did = await compact_history_with_summary(
                self.history, self.max_context_tokens, self.summarizer,
                self.keep_last_messages,
            )
        else:
            new_history, did = compact_history(
                self.history, self.max_context_tokens, self.keep_last_messages
            )
        if did:
            self.history = new_history
            if self.on_compact is not None:
                self.on_compact(before, len(self.history))

    def _subagent_handler(self, stream_id: str):
        """An event_stream_handler for a sub-agent run that forwards each event to
        the UI, tagged with ``stream_id`` so it can stream nested under the spawn.
        None when no UI is listening (headless) — the run just doesn't stream."""
        cb = self.deps.on_subagent_event
        if cb is None:
            return None

        async def handler(ctx, events) -> None:
            async for event in events:
                tokens = getattr(getattr(ctx, "usage", None), "total_tokens", 0) or 0
                await cb(stream_id, event, tokens)

        return handler

    def _build_subagent(self, type: str):
        """Build an isolated sub-agent of ``type`` on the current model, with its
        reach decided up front: gated tools only in auto mode, so a run never
        needs an approval round. Returns ``(agent, None)`` or, for an unknown
        type, ``(None, message)`` listing what's available."""
        defn = find_agent(self.deps.workspace_root, type)
        if defn is None:
            names = ", ".join(a.name for a in discover_agents(self.deps.workspace_root))
            return None, f"No sub-agent type {type!r}. Available: {names}."
        allow_gated = self.deps.mode is Mode.auto
        sub = Agent(
            self.current_model,
            deps_type=Deps,
            instructions=subagent_instructions(defn, self.deps.workspace_root),
        )
        self.provider.register_subagent(sub, effective_tools(defn, allow_gated=allow_gated))
        return sub, None

    async def _run_subagent(
        self, type: str, task: str, stream_id: str, mcp_names: list[str] | None = None
    ) -> str:
        """Spawn one isolated sub-agent of ``type``, run it to completion on
        ``task``, and return its final report — streaming its events to the UI
        nested under the spawn. Shares the workspace Deps (read-only use) but
        starts a fresh conversation, so the sub-agent gets a clean context.
        ``mcp_names`` is the MCP servers the main agent granted this spawn (none
        by default); granted servers gate via the same approval hook as the main
        agent's."""
        sub, err = self._build_subagent(type)
        if err is not None:
            return err
        granted, unknown = self._granted_servers(mcp_names)
        result = await sub.run(
            task, deps=self.deps, toolsets=granted,
            event_stream_handler=self._subagent_handler(stream_id),
        )
        # A foreground spawn runs inside the current turn, so its spend is folded
        # into the session total here and persisted by run_turn's _persist.
        self.usage += result.usage
        return self._mcp_grant_note(unknown) + result.output

    async def _run_background_subagent(
        self, type: str, task: str, mcp_names: list[str] | None = None
    ) -> str:
        """Run a sub-agent as a detached background job: same isolation, mode-based
        reach, and MCP grant as a foreground spawn, but with no event streaming —
        the job's result is its final report, surfaced when the agent pulls it.
        Any unknown-server note rides along on that report."""
        sub, err = self._build_subagent(type)
        if err is not None:
            return err
        granted, unknown = self._granted_servers(mcp_names)
        result = await sub.run(task, deps=self.deps, toolsets=granted)
        # A background spawn finishes off-turn, so no run_turn will fold in its
        # spend — count it here and persist right away so the saved session
        # reflects it even if the process exits before the next turn.
        self.usage += result.usage
        self._persist()
        return self._mcp_grant_note(unknown) + result.output

    @staticmethod
    def _server_name(server) -> str:
        """The display name of an MCP server — its tool prefix / config name."""
        return str(getattr(server, "id", None) or getattr(server, "tool_prefix", "?"))

    def _granted_servers(self, names: list[str] | None) -> tuple[list, list[str]]:
        """Resolve requested MCP server names to live server objects for a spawn.

        Returns ``(granted, unknown)``. ``granted`` is the live server objects
        whose name matches a request and is not disabled — passed straight to a
        sub-agent's run as toolsets, so their tools gate via the same approval
        hook as the main agent's. ``unknown`` is requested names with no enabled
        live server (missing or runtime-disabled). Order follows the request;
        duplicate names are honored once."""
        if not names:
            return [], []
        by_name = {self._server_name(s): s for s in self._live_servers}
        granted: list = []
        unknown: list[str] = []
        for name in dict.fromkeys(names):  # de-dupe, preserve first-seen order
            server = by_name.get(name)
            if server is None or name in self.disabled:
                unknown.append(name)
            else:
                granted.append(server)
        return granted, unknown

    def _enabled_server_names(self) -> list[str]:
        """Live MCP servers currently offered to the model — connected and not
        runtime-disabled. The set a spawn may grant from."""
        return [
            n for s in self._live_servers
            if (n := self._server_name(s)) not in self.disabled
        ]

    def mcp_index_text(self) -> str:
        """A spawn-time note listing the MCP servers a spawn may grant — the
        enabled live servers. Empty when none are enabled, so the instruction
        stays silent rather than mentioning a feature with nothing behind it."""
        names = self._enabled_server_names()
        if not names:
            return ""
        return (
            "MCP servers you can grant to a sub-agent via spawn_agent's `mcp` "
            "argument (e.g. mcp=[" + repr(names[0]) + "]): "
            + ", ".join(names)
        )

    def _mcp_grant_note(self, unknown: list[str]) -> str:
        """A short note for the model when a spawn requested MCP servers that
        couldn't be granted, naming what *is* enabled so it can re-spawn. Empty
        when nothing was unknown. Trailing blank line separates it from the
        sub-agent's report, which it is prepended to."""
        if not unknown:
            return ""
        bad = ", ".join(f"'{n}'" for n in unknown)
        enabled = self._enabled_server_names()
        avail = ", ".join(enabled) if enabled else "none"
        return f"(note: ignored unknown MCP server(s) {bad}; enabled: {avail})\n\n"

    def configured_names(self) -> list[str]:
        """Every configured MCP server name, enabled or not — what /mcp lists and
        what enable/disable ``all`` iterates over."""
        return [self._server_name(s) for s in self.mcp_servers]

    async def _connect_one(self, server) -> Optional[str]:
        """Open one server's connection into the shared stack, recording it live.
        Returns an error string on failure (the caller decides what to do), else
        None. Connections are refcounted, so a re-entered server is harmless."""
        if self._mcp_stack is None:
            self._mcp_stack = AsyncExitStack()
        try:
            await self._mcp_stack.enter_async_context(server)
        except Exception as exc:  # surfaced to the caller, never fatal
            return str(exc)
        self._live_servers.append(server)
        return None

    async def connect(self) -> dict:
        """Open connections to the enabled MCP servers, one at a time so a single
        failing server doesn't sink the rest. Servers in ``disabled`` are skipped
        — not launched at all. Connected servers join ``_live_servers`` (passed as
        per-run toolsets); failures are collected. Returns ``{"connected":
        [names], "failed": [(name, error)]}``. A no-op once already connected."""
        if self._connected or not self.mcp_servers:
            return self.mcp_status
        self._connected = True
        connected: list[str] = []
        failed: list[tuple[str, str]] = []
        for server in self.mcp_servers:
            name = self._server_name(server)
            if name in self.disabled:
                continue  # config-disabled: don't even launch it
            err = await self._connect_one(server)
            if err is None:
                connected.append(name)
            else:
                failed.append((name, err))
        self.mcp_status = {"connected": connected, "failed": failed}
        return self.mcp_status

    async def disable_server(self, name: str) -> None:
        """Turn a server off: its tools stop being offered to the model, and the
        choice is persisted to the config so it survives restarts. The live
        connection is kept this session (re-enabling is instant) and torn down
        with the rest at shutdown; once persisted, the next session won't even
        launch it."""
        self.disabled.add(name)
        persist_server_enabled(self.deps.workspace_root, name, False)

    async def enable_server(self, name: str) -> Optional[str]:
        """Turn a server back on: drop it from ``disabled``, persist the choice,
        and connect it if it isn't already live (the config-disabled case).
        Returns a connection-error string on failure (server left off), an
        explanatory string for an unknown name, or None on success."""
        self.disabled.discard(name)
        persist_server_enabled(self.deps.workspace_root, name, True)
        if any(self._server_name(s) == name for s in self._live_servers):
            return None
        server = next(
            (s for s in self.mcp_servers if self._server_name(s) == name), None
        )
        if server is None:
            return f"no such server {name!r}"
        err = await self._connect_one(server)
        if err is None:
            self.mcp_status["connected"].append(name)
            self.mcp_status["failed"] = [
                f for f in self.mcp_status["failed"] if f[0] != name
            ]
        return err

    async def aclose(self) -> None:
        """Close all live MCP connections. Safe to call when none were opened."""
        if self._mcp_stack is not None:
            await self._mcp_stack.aclose()
            self._mcp_stack = None
        self._live_servers = []
        self._connected = False

    async def run_turn(self, prompt: str, event_stream_handler=None) -> str:
        """Run the agent until it produces a final text answer, looping through
        any approval rounds. Returns the final text output."""
        await self._maybe_compact()
        digest = self.deps.jobs.take_finished_digest()
        if digest:
            prompt = f"{digest}\n\n{prompt}"
        user_prompt: Optional[str] = prompt
        deferred_results = None
        # Offer only the live servers that aren't disabled — a server muted at
        # runtime stays connected but its tools are withheld from the model.
        toolsets = [
            s for s in self._live_servers
            if self._server_name(s) not in self.disabled
        ]
        while True:
            result = await self.agent.run(
                user_prompt,
                model=self.current_model,
                message_history=self.history,
                deps=self.deps,
                deferred_tool_results=deferred_results,
                event_stream_handler=event_stream_handler,
                toolsets=toolsets,
            )
            self.history = result.all_messages()
            self.usage += result.usage
            self._persist()
            if isinstance(result.output, DeferredToolRequests):
                deferred_results = await resolve_approvals(
                    result.output, self.deps.mode, self.deps.request_approval
                )
                user_prompt = None  # continuation is driven by deferred_results
                continue
            output = result.output
            await self._maybe_autoname()
            return output
