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
    await app.post_system(f"Model: `{app.harness.model_label}`")


async def _cmd_exit(app: HarnessApp, arg: str) -> None:
    app.exit()


COMMANDS: list[Command] = [
    Command("help", "list available commands", _cmd_help, aliases=("?",)),
    Command("clear", "clear this conversation's history", _cmd_clear),
    Command("sessions", "list saved sessions", _cmd_sessions, aliases=("ls",)),
    Command("new", "start a new session: /new [name]", _cmd_new),
    Command("switch", "switch sessions: /switch <number|name>", _cmd_switch),
    Command("mode", "set approval mode: /mode [ask|auto|plan]", _cmd_mode),
    Command("model", "show the active model", _cmd_model),
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
