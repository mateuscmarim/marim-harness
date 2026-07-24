# superpowers

A marim adaptation of [obra/superpowers](https://github.com/obra/superpowers) —
its skills library (TDD, systematic debugging, brainstorming, plan writing and
execution, subagent-driven development, code review, and other collaboration
patterns) wired into marim's plugin system.

The 14 skills are namespaced `superpowers:<name>` once installed, e.g.
`superpowers:brainstorming`, `superpowers:systematic-debugging`,
`superpowers:test-driven-development`.

## Install

    marim plugin install examples/superpowers --link

Pure markdown skills plus one SessionStart hook — no MCP servers. The hook
injects a short bootstrap, so installing prompts once for trust; approve it to
let the plugin tell the model, from turn one, that the skills library exists.

## What marim adapts

This is a **trimmed vendor**: only the parts marim loads are checked in —
`skills/`, the marim-native hook, the `.marim-plugin` manifest, and the upstream
`LICENSE`. Upstream's tests, docs, and the Codex/Cursor/Kimi/OpenCode/Pi
harness configs are omitted; each skill keeps its own `scripts/` and
`references/`, so the set is self-contained.

The one behavioral change is the SessionStart hook. Upstream ships a Claude Code
hook (`hooks/session-start` + `run-hook.cmd`) that speaks CC's
`${CLAUDE_PLUGIN_ROOT}` JSON protocol. marim replaces it with
`hooks/marim-session-start.sh`, which reads `${MARIM_PLUGIN_ROOT}` and prints the
`using-superpowers` skill as plain-text `additionalContext` (marim takes a hook's
exit-0 stdout verbatim — no JSON envelope). The original CC hooks are not wired.

## Provenance & updating

Vendored from obra/superpowers **v6.2.0**. The skills carry no marim edits, so an
update is mechanical — replace `skills/` and `LICENSE` from a newer upstream tree
and bump the manifest version, keeping the marim hook and manifest. The
[`update-from-upstream.sh`](./update-from-upstream.sh) script does exactly that:

    ./update-from-upstream.sh ~/.claude/plugins/cache/claude-plugins-official/superpowers/<version>

Point it at any obra/superpowers checkout or marketplace cache at the target
version; it reads the version from upstream's own manifest, refreshes `skills/`
and `LICENSE`, and writes the new `version` into `.marim-plugin/plugin.json`.
Then review the diff (bump the version in this README by hand) and commit.

## License

MIT, © 2025 Jesse Vincent — see [`LICENSE`](./LICENSE). marim's adaptation
(the manifest and SessionStart hook) is contributed under the same terms.
