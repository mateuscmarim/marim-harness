#!/usr/bin/env bash
# marim-native SessionStart hook for the superpowers plugin.
#
# Injects the 'using-superpowers' skill as session context so the agent knows,
# from turn one, that the skills library exists and how to load skills. marim
# reads a hook's exit-0 stdout as injected context (plain text is taken
# verbatim as additionalContext — no JSON envelope required).
#
# This is the marim-native replacement for obra/superpowers' Claude Code
# SessionStart hook (hooks/session-start + run-hook.cmd), which targets CC's
# ${CLAUDE_PLUGIN_ROOT}/platform-branching JSON protocol. Here the command is
# invoked by marim with ${MARIM_PLUGIN_ROOT} substituted to the plugin's install
# dir; we also fall back to the script's own location so it works either way.
set -euo pipefail

ROOT="${MARIM_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
skill_file="$ROOT/skills/using-superpowers/SKILL.md"

# Fail safe: if the plugin moved or the skill is missing, inject nothing rather
# than erroring (marim ignores a non-zero hook, but exiting 0 with no output is
# the explicit "nothing to inject" signal).
[ -f "$skill_file" ] || exit 0
content="$(cat "$skill_file")" || exit 0

printf '%s\n' "<EXTREMELY_IMPORTANT>
You have superpowers — a library of skills installed as the marim 'superpowers' plugin.

Below is the full content of your 'superpowers:using-superpowers' skill: your introduction to finding and using skills. Skills from this plugin are namespaced 'superpowers:<name>'. When a task matches a skill's description, load that skill's full instructions on demand with the activate_skill tool (by its namespaced name) and follow them.

${content}
</EXTREMELY_IMPORTANT>"
exit 0
