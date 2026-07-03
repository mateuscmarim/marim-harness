"""Sub-agent definitions: built-in roles plus custom roles discovered from
``*.md`` files, reusing the skills discovery machinery (precedence roots,
frontmatter parsing, name dedup).

A sub-agent is launched by the main agent via the ``spawn_agent`` tool. It runs
in isolation on the same model, with a tool reach decided by its definition
intersected with the current mode (gated tools only in ``auto``). Two built-ins
always exist — ``explore`` (read-only) and ``general`` (full toolset). Custom
agents live in ``.marim/agents/<name>.md`` (and the parallel global
root); their file body is the role's system prompt and an optional ``tools:``
frontmatter line narrows the toolset. A custom file may reuse a built-in name to
override it.

Nothing here raises into a turn: a malformed definition (no frontmatter, bad
YAML, missing description, name/file mismatch, illegal name) is skipped.
"""

import dataclasses
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import builtin_root, config_dir
from ..tools.names import GATED_TOOLS, NET_TOOLS, READ_TOOLS, SUBAGENT_TOOLS
from ._discovery import cached_discover
from ._frontmatter import FRONTMATTER_RE
from .identifiers import valid_name

# What the built-in ``explore`` role may reach: local reads plus network egress
# (web lookups are genuinely useful mid-investigation), but nothing that mutates.
_EXPLORE_TOOLS = READ_TOOLS | NET_TOOLS

_EXPLORE_PROMPT = (
    "You are an exploration sub-agent. Investigate the workspace to answer the "
    "task you are given — read and search files as needed — then report your "
    "findings as your final message. Be specific: name files and line ranges. "
    "You may also use web_search and fetch_url to consult external docs when the "
    "answer isn't in the workspace. You cannot modify anything."
)
_GENERAL_PROMPT = (
    "You are a general-purpose sub-agent. Carry out the task you are given "
    "autonomously with your tools, then report what you did and any results as "
    "your final message. Read a file before editing it; keep changes minimal "
    "and focused."
)


@dataclass(frozen=True)
class AgentDef:
    """One sub-agent role: its identity, the system prompt that shapes it, and
    the tool names it may use (before the mode-based gating in effective_tools).
    ``source`` is ``built-in`` or the discovery root the file came from.
    ``plugin`` is the owning plugin's name when the agent came from a plugin."""

    name: str
    description: str
    prompt: str
    tools: frozenset[str]
    source: str
    plugin: str | None = None
    # Which runner executes this agent. "native" is the in-process Pydantic AI
    # loop; "claude-cli" spawns the Claude Code CLI (see subagents_cli.py). New
    # backends slot in here without touching discovery.
    backend: str = "native"
    # Backend-specific default model. For "claude-cli" this is a Claude Code model
    # name (e.g. "opus"/"sonnet" alias or a full id), passed verbatim to --model;
    # ignored by the native backend, which tracks the harness's runtime model.
    model: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}:{self.name}" if self.plugin else self.name


def _builtins() -> dict[str, AgentDef]:
    return {
        "explore": AgentDef(
            "explore",
            "Read-only investigation; reports findings, changes nothing. Use when "
            "investigating something before acting, especially over large files, "
            "logs, or output you don't want cluttering your own context.",
            _EXPLORE_PROMPT, _EXPLORE_TOOLS, "built-in",
        ),
        "general": AgentDef(
            "general",
            "Full toolset; carries out a focused sub-task autonomously.",
            _GENERAL_PROMPT, SUBAGENT_TOOLS, "built-in",
        ),
    }


def agent_roots(workspace_root) -> list[tuple[str, Path]]:
    """The discovery roots, highest precedence first: project, then global, then
    marim's bundled built-in agents."""
    ws = Path(workspace_root)
    return [
        ("project", ws / ".marim" / "agents"),
        ("global", config_dir() / "agents"),
        ("builtin", builtin_root() / "agents"),
    ]


def _opt_str(raw: object, default: str | None) -> str | None:
    """Return the stripped string value of ``raw`` when non-empty, else ``default``."""
    if isinstance(raw, str) and (s := raw.strip()):
        return s
    return default


def _parse_tools(raw) -> frozenset[str]:
    """Read a ``tools:`` frontmatter value (comma string or YAML list) into the
    known sub-agent tools. Unknown names are dropped; an empty/absent value
    defaults to the read-only set."""
    if raw is None:
        return READ_TOOLS
    if isinstance(raw, str):
        names = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        names = [str(part).strip() for part in raw]
    else:
        return READ_TOOLS
    allowed = frozenset(n for n in names if n in SUBAGENT_TOOLS)
    return allowed or READ_TOOLS


def _parse_agent(source: str, path: Path, plugin: str | None = None) -> AgentDef | None:
    """Build an AgentDef from a ``<name>.md`` file, or None if invalid. The file
    stem is the authoritative identity; frontmatter must carry a non-empty
    description and, if it names the agent, must match the stem."""
    name = path.stem
    if not valid_name(name):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    fm_name = data.get("name")
    if fm_name is not None and str(fm_name) != name:
        return None
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    backend = _opt_str(data.get("backend"), "native") or "native"
    model = _opt_str(data.get("model"), None)
    return AgentDef(
        name=name,
        description=description.strip(),
        prompt=match.group(2).strip(),
        tools=_parse_tools(data.get("tools")),
        source=source,
        plugin=plugin,
        backend=backend,
        model=model,
    )


def _all_roots(workspace_root) -> list[tuple[str, Path, str | None]]:
    """The discovery roots in precedence order as ``(source, root, plugin)``:
    user roots (project, then global), then plugin roots."""
    from ..plugins import plugin_agent_roots

    roots: list[tuple[str, Path, str | None]] = [
        (source, root, None) for source, root in agent_roots(workspace_root)
    ]
    roots += [
        (f"plugin:{name}", root, name)
        for name, root in plugin_agent_roots(workspace_root)
    ]
    return roots


# Cache of discovery results keyed by workspace root. Each entry holds the
# filesystem signature it was computed from; a discovery is reused only while
# that signature still matches, so an added/removed/edited agent file is picked
# up on the next call without re-reading and re-parsing every file each turn.
_DISCOVERY_CACHE: dict[str, tuple[tuple, list[AgentDef]]] = {}


def _discovery_signature(roots: list[tuple[str, Path, str | None]]) -> tuple:
    """A cheap fingerprint of every candidate ``*.md`` across the roots: each
    file's name, mtime, and size. Changes whenever a file is added, removed, or
    edited — stat-only, so it skips the expensive read+YAML parse on a cache hit."""
    sig: list = []
    for source, root, _plugin in roots:
        try:
            paths = sorted(
                p for p in root.iterdir() if p.is_file() and p.suffix == ".md"
            )
        except OSError:
            sig.append((source, str(root), None))
            continue
        files = []
        for p in paths:
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((p.name, st.st_mtime_ns, st.st_size))
        sig.append((source, str(root), tuple(files)))
    return tuple(sig)


def discover_agents(workspace_root) -> list[AgentDef]:
    """All effective sub-agents: custom definitions (user roots first, then
    plugin roots as ``plugin:name``) layered over the built-ins, deduped by
    qualified name with the highest-precedence root winning. Sorted by
    qualified name for stable display.

    Cached per workspace root and reused while the agent files on disk are
    unchanged (by name/mtime/size), so repeated calls within a turn — and across
    turns that didn't touch an agent file — don't re-walk and re-parse them."""
    # Resolve the key so different spellings of the same dir (symlinks, trailing
    # slash, relative vs absolute) share one cache entry instead of duplicating.
    roots = _all_roots(workspace_root)
    return cached_discover(
        workspace_root, roots,
        _discovery_signature,
        _collect_agents,
        lambda a: a.qualified_name,
        _DISCOVERY_CACHE,
        _builtins(),
    )


def _collect_agents(seen: dict, source: str, root: Path, plugin: str | None) -> None:
    try:
        files = sorted(p for p in root.iterdir() if p.is_file() and p.suffix == ".md")
    except OSError:
        return
    for path in files:
        agent = _parse_agent(source, path, plugin=plugin)
        if agent is None:
            continue
        if agent.qualified_name in seen:
            continue  # a higher-precedence root already claimed this name
        seen[agent.qualified_name] = agent


def find_agent(workspace_root, name: str) -> AgentDef | None:
    """The effective sub-agent whose qualified name is ``name``, or None."""
    for agent in discover_agents(workspace_root):
        if agent.qualified_name == name:
            return agent
    return None


def effective_tools(defn: AgentDef, *, allow_gated: bool) -> frozenset[str]:
    """The tool names a spawn should actually grant: the definition's tools, with
    workspace-mutating (gated) tools removed unless the mode allows them (auto).
    Network tools are not gated here — they're decided by the definition itself."""
    if allow_gated:
        return defn.tools
    return defn.tools - GATED_TOOLS


def subagent_instructions(
    defn: AgentDef, workspace_root, max_output_chars: int | None = None
) -> str:
    """The system prompt for a spawned sub-agent: its role plus where it works,
    and — when the spawner set one — a soft output budget it should distill
    toward. The budget is a target the model summarizes for, not a guillotine on
    its return value; ``cap_subagent_output`` is the lossless backstop."""
    base = (
        f"{defn.prompt}\n\nYou are operating inside the workspace at "
        f"{workspace_root}. All file paths are relative to it."
    )
    if max_output_chars is not None:
        base += _output_budget_instruction(max_output_chars)
    return base


def _output_budget_instruction(max_output_chars: int) -> str:
    """The soft-target budget line appended to a sub-agent's instructions: name
    the size, lead with the conclusion, summarize to fit rather than overrun."""
    return (
        f"\n\nOutput budget: keep your final report to about {max_output_chars} "
        "characters. Lead with the conclusion, then supporting detail in "
        "descending importance. Summarize to fit — do not pad, and do not get "
        "cut off mid-thought; if there is more than fits, give the key findings "
        "and note what you left out. A hard backstop applies: anything over "
        "budget is moved to a file and replaced with a pointer, so put what "
        "matters first."
    )


def compose_subagent_task(
    task: str,
    *,
    returns: str | None = None,
    constraints: str | None = None,
    context: str | None = None,
) -> str:
    """Assemble a spawn's freeform ``task`` with the optional structured fields
    the spawner can attach, into one prompt for the sub-agent. The fields are a
    checklist for the *delegator* — the things a clean-context sub-agent most
    often isn't told: what to hand back (``returns``), what not to do
    (``constraints``, soft — real reach is set by tool grants), and the
    orchestrator-only background it can't see (``context``).

    Additive and order-stable: with no fields set (or only blank ones), the task
    is returned untouched, so the simple case stays one string. Sections appear
    in a fixed order — task, then context, constraints, return — each only when
    its field has content."""
    sections = [task.rstrip()]
    for label, value in (
        ("Context", context),
        ("Constraints", constraints),
        ("Return", returns),
    ):
        if value is not None and value.strip():
            sections.append(f"{label}:\n{value.strip()}")
    return "\n\n".join(sections)


def cap_subagent_output(
    output: str, max_output_chars: int | None, spill_path: str
) -> tuple[str, str | None]:
    """Enforce a spawner-set output cap losslessly. Returns ``(text, spill)``:
    ``text`` is what the main agent receives, ``spill`` is the full output to
    write to ``spill_path`` (``None`` when nothing needs spilling).

    Under budget (or no cap) the output passes through untouched. Over budget,
    the full output is returned as ``spill`` for the caller to persist, and
    ``text`` is the head of the report — where the sub-agent was told to front-
    load the conclusion — plus a pointer to the file, kept within the budget so
    the cap the spawner asked for actually holds."""
    if max_output_chars is None or len(output) <= max_output_chars:
        return output, None
    note = f"\n\n[output capped at {max_output_chars} chars — full report at {spill_path}]"
    head = output[: max(0, max_output_chars - len(note))]
    return head + note, output


def cap_transcript(messages: list, cap: int, *, cap_reasoning: bool = False) -> list:
    """Return a copy of ``messages`` with every ``ToolReturnPart`` whose content
    exceeds ``cap`` characters truncated to ``cap`` chars plus a marker. Only tool
    *results* are capped by default — text, thinking, and tool-call parts (the
    reasoning and the actions) are kept in full. Pure: never mutates the input.

    ``cap_reasoning`` additionally clips oversized ``TextPart`` / ``ThinkingPart``
    contents to the same per-part cap. It exists for the *checkpoint* path only
    (see ``TranscriptStore.write`` / the runner's checkpoint closure): a mid-run
    sidecar is re-serialized before EVERY model request on the event loop, so an
    unbounded stream of reasoning would make each checkpoint's payload grow with
    the whole conversation. Final writes leave it False so a completed sidecar
    keeps its full reasoning."""
    # Imported lazily so this module stays free of pydantic_ai at import time: the
    # CLI router pulls in workspace/ (via config → catalog), and dragging in
    # pydantic_ai there would cost ~1s on every `marim --help`/config command.
    from pydantic_ai.messages import TextPart, ThinkingPart, ToolReturnPart

    def _clip(text: str) -> str:
        marker = f"\n…(truncated, {len(text)} chars)"
        return text[: max(0, cap - len(marker))] + marker

    out = []
    for message in messages:
        parts = getattr(message, "parts", None)
        if not parts:
            out.append(message)
            continue
        # Tool results always cap; text/thinking cap only on the checkpoint path.
        cappable = (ToolReturnPart, TextPart, ThinkingPart) if cap_reasoning else ToolReturnPart
        new_parts = []
        for part in parts:
            if isinstance(part, cappable):
                text = part.content if isinstance(part.content, str) else str(part.content)
                if len(text) > cap:
                    if isinstance(part, ThinkingPart):
                        # The provider's signature validates the FULL original
                        # content; once we've clipped it, the signature no longer
                        # matches. pydantic-ai re-sends a thinking block verbatim
                        # whenever provider_name matches and signature is not
                        # None, so a stale signature on truncated content 400s on
                        # the very next resumed request. Null it — pydantic-ai
                        # then omits the block instead of re-sending it broken.
                        part = dataclasses.replace(
                            part, content=_clip(text), signature=None
                        )
                    else:
                        part = dataclasses.replace(part, content=_clip(text))
            new_parts.append(part)
        out.append(dataclasses.replace(message, parts=new_parts))
    return out


def agents_index_text(defs: list[AgentDef]) -> str:
    """The injected index of spawnable sub-agents: one ``- name — description``
    line each. Never empty — the built-ins are always present."""
    return "\n".join(f"- {a.qualified_name} — {a.description}" for a in defs)
