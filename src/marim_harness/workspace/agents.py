"""Sub-agent definitions: built-in roles plus custom roles discovered from
``*.md`` files, reusing the skills discovery machinery (precedence roots,
frontmatter parsing, name dedup).

A sub-agent is launched by the main agent via the ``spawn_agent`` tool. It runs
in isolation on the same model, with a tool reach decided by its definition
intersected with the current mode (gated tools only in ``auto``). Two built-ins
always exist — ``explore`` (read-only) and ``general`` (full toolset). Custom
agents live in ``.marim/agents/<name>.md`` (and the parallel claude/global
roots); their file body is the role's system prompt and an optional ``tools:``
frontmatter line narrows the toolset. A custom file may reuse a built-in name to
override it.

Nothing here raises into a turn: a malformed definition (no frontmatter, bad
YAML, missing description, name/file mismatch, illegal name) is skipped.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import config_dir
from ..tools.names import GATED_TOOLS, NET_TOOLS, READ_TOOLS, SUBAGENT_TOOLS

# What the built-in ``explore`` role may reach: local reads plus network egress
# (web lookups are genuinely useful mid-investigation), but nothing that mutates.
_EXPLORE_TOOLS = READ_TOOLS | NET_TOOLS

# Same identifier rules as skills: 1-64 chars, lowercase alphanumerics, single
# hyphens, no leading/trailing/consecutive hyphens.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

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
    ``source`` is ``built-in`` or the discovery root the file came from."""

    name: str
    description: str
    prompt: str
    tools: frozenset[str]
    source: str


def _builtins() -> dict[str, AgentDef]:
    return {
        "explore": AgentDef(
            "explore",
            "Read-only investigation; reports findings, changes nothing.",
            _EXPLORE_PROMPT, _EXPLORE_TOOLS, "built-in",
        ),
        "general": AgentDef(
            "general",
            "Full toolset; carries out a focused sub-task autonomously.",
            _GENERAL_PROMPT, SUBAGENT_TOOLS, "built-in",
        ),
    }


def agent_roots(workspace_root) -> list[tuple[str, Path]]:
    """The four discovery roots, highest precedence first: project over global,
    marim over claude within each scope."""
    ws = Path(workspace_root)
    return [
        ("project", ws / ".marim" / "agents"),
        ("project/.claude", ws / ".claude" / "agents"),
        ("global", config_dir() / "agents"),
        ("global/.claude", Path.home() / ".claude" / "agents"),
    ]


def _valid_name(name: str) -> bool:
    return bool(name) and len(name) <= 64 and _NAME_RE.match(name) is not None


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


def _parse_agent(source: str, path: Path) -> AgentDef | None:
    """Build an AgentDef from a ``<name>.md`` file, or None if invalid. The file
    stem is the authoritative identity; frontmatter must carry a non-empty
    description and, if it names the agent, must match the stem."""
    name = path.stem
    if not _valid_name(name):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = _FRONTMATTER_RE.match(text)
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
    return AgentDef(
        name=name,
        description=description.strip(),
        prompt=match.group(2).strip(),
        tools=_parse_tools(data.get("tools")),
        source=source,
    )


def discover_agents(workspace_root) -> list[AgentDef]:
    """All effective sub-agents for a workspace: custom definitions (deduped by
    name, highest-precedence root winning) layered over the built-ins, which fill
    any name a custom file didn't claim. Sorted by name for stable display."""
    seen: dict[str, AgentDef] = {}
    for source, root in agent_roots(workspace_root):
        try:
            files = sorted(
                p for p in root.iterdir() if p.is_file() and p.suffix == ".md"
            )
        except OSError:
            continue
        for path in files:
            if path.stem in seen:
                continue  # a higher-precedence root already claimed this name
            agent = _parse_agent(source, path)
            if agent is not None:
                seen[agent.name] = agent
    for name, agent in _builtins().items():
        seen.setdefault(name, agent)
    return sorted(seen.values(), key=lambda a: a.name)


def find_agent(workspace_root, name: str) -> AgentDef | None:
    """The effective sub-agent with ``name``, or None."""
    for agent in discover_agents(workspace_root):
        if agent.name == name:
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


def agents_index_text(defs: list[AgentDef]) -> str:
    """The injected index of spawnable sub-agents: one ``- name — description``
    line each. Never empty — the built-ins are always present."""
    return "\n".join(f"- {a.name} — {a.description}" for a in defs)
