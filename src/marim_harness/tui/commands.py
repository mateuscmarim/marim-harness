"""Slash-command framework for the TUI.

A command is a name plus aliases, a one-line summary, and an async handler that
takes the app and the argument string. ``dispatch`` parses ``/name arg`` and
routes it; anything not matching a known command reports an error rather than
being sent to the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from ..permissions import Mode
from ..skills import discover_skills
from .themes import THEME_NAMES

if TYPE_CHECKING:
    from .app import HarnessApp

Handler = Callable[["HarnessApp", str], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    handler: Handler
    aliases: tuple[str, ...] = field(default_factory=tuple)


async def _cmd_help(app: HarnessApp, arg: str) -> None:
    lines = ["**Commands**", ""]
    for cmd in COMMANDS:
        names = "/" + cmd.name
        if cmd.aliases:
            names += " " + ", ".join("/" + a for a in cmd.aliases)
        lines.append(f"- `{names}` — {cmd.summary}")
    lines += [
        "",
        "Drop an `AGENTS.md` in the workspace root for project-specific "
        "instructions; it's re-read every turn.",
    ]
    await app.post_system("\n".join(lines))


async def _cmd_clear(app: HarnessApp, arg: str) -> None:
    await app.reset_conversation()


def resolve_ref(infos: list, ref: str) -> object | None:
    """Find a session by 1-based list position, exact id, or exact name
    (case-insensitive). Returns the matching SessionInfo or None."""
    ref = ref.strip()
    if not ref:
        return None
    if ref.isdigit():
        index = int(ref) - 1
        return infos[index] if 0 <= index < len(infos) else None
    for info in infos:
        if info.id == ref:
            return info
    for info in infos:
        if info.name.lower() == ref.lower():
            return info
    return None


async def _cmd_sessions(app: HarnessApp, arg: str) -> None:
    infos = app.harness.sessions()
    if not infos:
        await app.post_system("No saved sessions yet. Use `/new [name]` to start one.")
        return
    active = getattr(app.harness, "session_name", None)
    lines = ["**Sessions**", ""]
    for i, info in enumerate(infos, start=1):
        marker = " ← active" if info.name == active else ""
        when = info.updated[:16].replace("T", " ") if info.updated else "—"
        lines.append(
            f"{i}. `{info.name}` — {info.message_count} msgs, "
            f"{info.tokens} tokens, {when}{marker}"
        )
    lines += ["", "Switch with `/switch <number|name>`."]
    await app.post_system("\n".join(lines))


async def _cmd_new(app: HarnessApp, arg: str) -> None:
    await app.start_new_session(arg.strip() or None)


async def _cmd_name(app: HarnessApp, arg: str) -> None:
    new = await app.harness.rename_session(arg.strip() or None)
    if new is None:
        await app.post_system(
            "Couldn't name the session — give a title (`/name <title>`) or have a "
            "conversation first so it can be auto-titled."
        )
        return
    app._refresh_status()
    await app.post_system(f"Renamed session to `{new}`.")


async def _cmd_switch(app: HarnessApp, arg: str) -> None:
    ref = arg.strip()
    if not ref:
        await app.post_system("Usage: `/switch <number|name>`. See `/sessions`.")
        return
    info = resolve_ref(app.harness.sessions(), ref)
    if info is None:
        await app.post_system(f"No session matches `{ref}`. Try `/sessions`.")
        return
    await app.switch_to_session_id(info.id)


async def _cmd_mode(app: HarnessApp, arg: str) -> None:
    arg = arg.strip().lower()
    if not arg:
        app.harness.deps.mode = app.harness.deps.mode.cycle()
    else:
        try:
            app.harness.deps.mode = Mode(arg)
        except ValueError:
            await app.post_system(f"Unknown mode: `{arg}`. Use ask, auto, or plan.")
            return
    app._refresh_status()
    await app.post_system(f"Mode: **{app.harness.deps.mode.value}**")


async def _cmd_model(app: HarnessApp, arg: str) -> None:
    arg = arg.strip()
    if arg:
        app.harness.set_model(arg)
        app._refresh_status()
        await app.post_system(f"Model: `{app.harness.model_label}`")
        return
    await app.open_model_picker()


async def _cmd_theme(app: HarnessApp, arg: str) -> None:
    """List the available themes, or switch to one: ``/theme [name]``."""
    name = arg.strip()
    if not name:
        current = getattr(app, "theme", None)
        lines = ["**Themes**", ""]
        for t in THEME_NAMES:
            marker = " ← active" if t == current else ""
            lines.append(f"- `{t}`{marker}")
        lines += ["", "Switch with `/theme <name>` (or `Ctrl+P` → Change theme)."]
        await app.post_system("\n".join(lines))
        return
    if name not in THEME_NAMES:
        await app.post_system(
            f"Unknown theme: `{name}`. Available: {', '.join(THEME_NAMES)}."
        )
        return
    app.theme = name  # the app's watch_theme persists the choice


async def _cmd_remember(app: HarnessApp, arg: str) -> None:
    arg = arg.strip()
    if not arg:
        await app.post_system(
            "Usage: `/remember <fact>` — saves a durable note to memory. "
            "The agent picks the scope (project vs global), type, and title."
        )
        return
    prompt = (
        "Save the following to persistent memory by calling the remember tool. "
        "Pick an appropriate scope (project vs global), type, and a concise title "
        f"and one-line description.\n\nFact: {arg}"
    )
    app._current_assistant = None
    app._turn_worker = app.run_worker(app._run_turn(prompt), exclusive=True)


async def _cmd_skill(app: HarnessApp, arg: str) -> None:
    arg = arg.strip()
    if not arg:
        skills = discover_skills(app.harness.deps.workspace_root)
        if not skills:
            await app.post_system(
                "No skills found. Drop a skill directory under `.marim/skills/` "
                "(or `.claude/skills/`) with a `SKILL.md` inside."
            )
            return
        lines = ["**Skills**", ""]
        for s in skills:
            tag = " _(manual-only)_" if s.disable_model_invocation else ""
            lines.append(f"- `{s.name}` — {s.description}{tag}")
        lines += ["", "Run one with `/skill <name> [extra context]`."]
        await app.post_system("\n".join(lines))
        return
    name, _, extra = arg.partition(" ")
    extra = extra.strip()
    prompt = (
        f"Activate the skill named '{name}' by calling the activate_skill tool, "
        "then carry out its instructions."
    )
    if extra:
        prompt += f"\n\nAdditional context for this run: {extra}"
    app._current_assistant = None
    app._turn_worker = app.run_worker(app._run_turn(prompt), exclusive=True)


_MCP_USAGE = "Usage: `/mcp`, `/mcp enable <name|all>`, `/mcp disable <name|all>`."


async def _cmd_mcp(app: HarnessApp, arg: str) -> None:
    action, _, rest = arg.strip().partition(" ")
    action = action.lower()
    if action in ("enable", "disable"):
        await _mcp_toggle(app, action, rest.strip())
        return
    if action:
        await app.post_system(_MCP_USAGE)
        return
    await _mcp_list(app)


async def _mcp_list(app: HarnessApp) -> None:
    servers = getattr(app.harness, "mcp_servers", [])
    if not servers:
        await app.post_system(
            "No MCP servers configured. Add them to `.marim/mcp.json` (project) "
            "or `~/.config/marim/mcp.json` (global)."
        )
        return
    status = getattr(app.harness, "mcp_status", {"connected": [], "failed": []})
    connected = set(status.get("connected", []))
    failed = dict(status.get("failed", []))
    disabled = set(getattr(app.harness, "disabled", set()) or set())
    lines = ["**MCP servers**", ""]
    for s in servers:
        name = str(getattr(s, "id", None) or getattr(s, "tool_prefix", "?"))
        if name in disabled:
            state = "disabled ⏸"
        elif name in connected:
            state = "connected ✓"
        elif name in failed:
            state = f"failed ✗ — {failed[name]}"
        else:
            state = "not connected"
        lines.append(f"- `{name}` — {state}")
    lines += ["", "Toggle with `/mcp enable <name|all>` or `/mcp disable <name|all>`."]
    await app.post_system("\n".join(lines))


async def _mcp_toggle(app: HarnessApp, action: str, target: str) -> None:
    names = app.harness.configured_names()
    if not names:
        await app.post_system("No MCP servers configured.")
        return
    listing = ", ".join(f"`{n}`" for n in names)
    if not target:
        await app.post_system(f"Usage: `/mcp {action} <name|all>`. Configured: {listing}.")
        return
    if target == "all":
        targets = names
    elif target in names:
        targets = [target]
    else:
        await app.post_system(f"No MCP server `{target}`. Configured: {listing}.")
        return
    results = []
    for name in targets:
        if action == "disable":
            await app.harness.disable_server(name)
            results.append(f"- `{name}` — disabled ⏸")
        else:
            err = await app.harness.enable_server(name)
            if err:
                results.append(f"- `{name}` — enable failed ✗ — {err}")
            else:
                results.append(f"- `{name}` — enabled ✓")
    heading = "**MCP disabled**" if action == "disable" else "**MCP enabled**"
    await app.post_system("\n".join([heading, "", *results]))


async def _cmd_exit(app: HarnessApp, arg: str) -> None:
    app.exit()


COMMANDS: list[Command] = [
    Command("help", "list available commands", _cmd_help, aliases=("?",)),
    Command("clear", "clear this conversation's history", _cmd_clear),
    Command("sessions", "list saved sessions", _cmd_sessions, aliases=("ls",)),
    Command("new", "start a new session: /new [name]", _cmd_new),
    Command("switch", "switch sessions: /switch <number|name>", _cmd_switch),
    Command("name", "name this session: /name [title] (auto-titles if blank)", _cmd_name),
    Command("mode", "set approval mode: /mode [ask|auto|plan]", _cmd_mode),
    Command("model", "switch model: /model [id] (opens a picker if blank)", _cmd_model),
    Command("theme", "list or set the color theme: /theme [name]", _cmd_theme),
    Command("remember", "save a note to memory: /remember <fact>", _cmd_remember),
    Command("skill", "list or run skills: /skill [name [context]]", _cmd_skill),
    Command("mcp", "list MCP servers or toggle them: /mcp [enable|disable <name|all>]", _cmd_mcp),
    Command("exit", "quit the harness", _cmd_exit, aliases=("quit",)),
]

COMMANDS_BY_NAME: dict[str, Command] = {}
for _cmd in COMMANDS:
    COMMANDS_BY_NAME[_cmd.name] = _cmd
    for _alias in _cmd.aliases:
        COMMANDS_BY_NAME[_alias] = _cmd


async def dispatch(app: HarnessApp, text: str) -> None:
    """Run the slash command in ``text`` (which starts with '/')."""
    name, _, arg = text[1:].partition(" ")
    cmd = COMMANDS_BY_NAME.get(name.lower())
    if cmd is None:
        await app.post_system(f"Unknown command: `/{name}`. Try `/help`.")
        return
    await cmd.handler(app, arg.strip())
