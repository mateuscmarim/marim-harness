# Skills, memory, and instructions

marim gives you four ways to shape what the agent knows beyond the code itself:

- **AGENTS.md instructions** — standing rules, always in the prompt.
- **Skills** — packaged workflows the agent loads on demand.
- **Memory** — durable facts the agent saves and recalls itself.
- **The scratchpad** — a per-session temp directory for intermediate files.

This guide covers where each lives on disk, its file format, and when to reach
for which.

## AGENTS.md instructions

Two instruction files are injected into the system prompt, in this order:

1. **Global** — `~/.config/marim/AGENTS.md` (respects `XDG_CONFIG_HOME`).
   Applies to every project; labeled "apply to every project" in the prompt.
2. **Project** — `<workspace>/AGENTS.md`, falling back to
   `<workspace>/CLAUDE.md`. The first non-empty file wins; the other is
   ignored. Labeled "Project-specific instructions".

Because the project block appears after the global one, project instructions
effectively take precedence when the two conflict — the model sees both, with
the project's rules last.

A missing, empty, or unreadable file is simply skipped — it never breaks a
turn. The files are re-read whenever they change on disk (the read is memoized
on the file's mtime and size), so you can edit `AGENTS.md` mid-session and the
next request picks it up. No restart needed.

Installed plugins may contribute their own `AGENTS.md`, injected as
"Instructions contributed by installed plugins" and treated like project
instructions. Project-scope plugins only load when the project is trusted (see
the trust gate below).

## Skills

A skill is a packaged, on-demand workflow following the
[agentskills.io](https://agentskills.io) open standard: a **directory whose
name is the skill's identity**, containing a `SKILL.md` (YAML frontmatter +
markdown body) and optionally bundling `scripts/`, `references/`, and
`assets/`. Skill names are kebab-case: 1-64 chars, lowercase alphanumerics
and single hyphens (`my-skill`, not `My_Skill`).

### Where skills are discovered

Roots are scanned in precedence order — the first root to claim a name wins:

1. **Project** — `<workspace>/.marim/skills/` (only when the project is
   trusted; see below).
2. **Global** — `~/.config/marim/skills/`.
3. **Built-in** — skills bundled inside the marim package (currently
   `deep-research`).
4. **Plugins** — skills shipped by installed plugins, namespaced as
   `plugin-name:skill-name`. A user's own skill always beats a plugin's
   same-named one, since plugin roots come after user roots.

**Trust gate:** project-local skills are injected into the system prompt every
turn, so a cloned untrusted repo could prompt-inject the agent through a
committed `.marim/skills/` directory. Project skills (and project-scope plugin
skills) therefore load only when `MARIM_TRUST_PROJECT_HOOKS` is set truthy
(`1`, `true`, `on`, `yes`) — the same gate as project hooks and MCP. Global
and built-in skills always load.

### SKILL.md format

```markdown
---
name: my-skill            # optional; if present, must match the directory name
description: One line saying when to use this skill.   # required, non-empty
disable-model-invocation: false   # optional; true = manual-only (/skill)
allowed-tools: ""                 # optional; parsed but not enforced in v1
metadata: {}                      # optional free-form dict
---

The full instructions the agent follows once the skill is activated.
```

Only `description` is required. A malformed skill — no `SKILL.md`, bad YAML,
missing description, a `name` that doesn't match the directory, or an illegal
directory name — is silently skipped, never fatal.

### How skills are invoked

Discovery is cheap: a one-line `name — description` index is injected into the
prompt each turn (skills marked `disable-model-invocation` are left out so the
model won't auto-activate them). When a task matches a description, the model
calls:

- `activate_skill(name)` — returns the full `SKILL.md` body plus the skill's
  absolute directory. Oversized bodies are spilled to a file with a preview
  rather than flooding the context.
- `read_skill_file(name, path)` — reads a bundled file (e.g.
  `references/REFERENCE.md`) by path relative to the skill directory, guarded
  against escaping it. Works for global skills outside the workspace too.

Bundled scripts run through the ordinary `bash` tool using the absolute path
surfaced on activation, so they inherit its normal approval gating.

From the TUI, `/skill` lists all discovered skills (manual-only ones tagged),
and `/skill <name> [extra context]` starts a turn instructing the agent to
activate that skill and carry out its instructions.

## Memory

Memory is native markdown, mirroring Claude Code's design. It lives in two
scopes with the same shape:

- **Global** — `~/.config/marim/memory/` — per-user facts that hold in every
  workspace.
- **Project** — `<workspace>/.marim/memory/` — facts about this codebase,
  committable alongside the repo.

Each scope holds a small `MEMORY.md` index (one line per fact:
`- [Title](slug.md) — hook`) plus one `<slug>.md` file per fact carrying YAML
frontmatter (`name`, `description`, `metadata.type`) and a markdown body. The
type is one of `user`, `feedback`, `project`, `reference`. Titles are slugified
to filenames (accents transliterated, so `usuário` and `usuario` collapse to
one entry).

The index is injected into the system prompt each turn — it's tiny — while
full bodies load on demand. Memory files live in marim's own directories (the
global scope is outside the workspace), so they are **not reachable through
`read_file`**; the agent uses `recall` instead.

An embedder using `HarnessBuilder.with_memory` can point both scopes under one
explicit root instead of the CLI defaults above.

### The three tools

- **`remember(title, description, body, scope, type)`** — saves a fact.
  Saving with an existing title updates that entry in place (the index line is
  upserted, never duplicated).
- **`recall(name, scope)`** — reads a memory's full body by title or slug.
- **`forget(name, scope)`** — permanently deletes a memory and drops its index
  line. For correcting a fact, `remember` with the same title is preferred;
  `forget` is for entries that are wrong or obsolete.

`scope` defaults to `"project"` in all three. None of them require approval —
they only touch marim's own memory directories, and every failure is soft (a
read-only directory produces an explanatory tool result, never a crashed
turn).

### Wikilinks

Memory bodies may link related memories with `[[name]]`. Links are flat (a
title or a slug — both resolve the same way), and a dangling link is not an
error: by convention it marks a fact worth writing later. When a recalled body
contains links, `recall` appends a one-line footer such as:

```
Linked memories — saved: tui-redesign; not yet written: settings-port. Read saved ones with recall.
```

Existence is checked against the actual `<slug>.md` files, not the index, so
the footer can't be fooled by a stale index line.

### Store hygiene

The tool docstrings themselves carry the hygiene rules the model follows:

- Make `description` self-contained — it is the only line in the always-loaded
  index, so it should state the fact, not label it.
- Use absolute dates (`2026-07-22`), never relative ones.
- Don't save what the repo already records — git history, `AGENTS.md`, code
  structure.
- Check the index before saving and reuse an existing title to update rather
  than duplicate.
- Memories reflect when they were written — verify a file or flag a memory
  names still exists before acting on it.

### /remember and proactive memory

`/remember <fact>` in the TUI starts a turn instructing the agent to save the
fact via the `remember` tool, picking the scope, type, and title itself.

By default the agent saves **only when explicitly asked** (`"remember
that…"` or `/remember`). Set `MARIM_PROACTIVE_MEMORY=1` (default off; also a
toggle in the TUI Settings screen) to switch the injected policy: the agent
then proactively saves durable user preferences, feedback, and project
conventions — still skipping one-off details, secrets, and anything the repo
records — and updates or forgets entries instead of accumulating duplicates.

## The session scratchpad

Each session gets a temp directory **outside the workspace** for intermediate
files — working scripts, staged outputs, analysis artifacts — so they don't
pollute the project tree or its git status:

```
<tmpdir>/marim-<uid>/<workspace-name>-<hash>/<session-id>/scratchpad
```

(`<tmpdir>` is the system temp dir, normally `/tmp`; `<hash>` is a 12-char
digest of the workspace path, matching how session storage keys workspaces.)

How it behaves:

- The absolute path is advertised in the system prompt each turn, with
  instructions to use it for temporary files instead of the workspace.
- The file tools reach it as an **extra guard root**: `read_file`,
  `write_file`, and `edit_file` accept absolute paths inside the scratchpad in
  addition to the workspace.
- In **ask mode**, `write_file`/`edit_file` calls that resolve inside the
  scratchpad are auto-approved — that is the point of the directory. The
  resolution chases symlinks and `..`, so only a real scratchpad target
  qualifies. `bash` never gets this bypass, since a command's filesystem reach
  can't be cheaply proven to stay inside the scratchpad.
- Gated by `MARIM_SCRATCHPAD` (default **on**). It also disables itself when
  it can't be provided safely: the per-user base (`marim-<uid>`) is created
  mode 0700 and refused if it turns out to be a symlink or owned by another
  user (classic /tmp squatting).
- **Lifetime:** the directory is removed when the session is deleted, and
  ordinary /tmp semantics reclaim it on reboot — anything worth keeping
  belongs in the workspace. Compaction also uses it (an `elided/`
  subdirectory) to preserve large tool outputs it trims from the transcript.

## Choosing between them

**Skills vs AGENTS.md.** `AGENTS.md` is always in context, every turn — use it
for short standing rules that apply to most work in the project (build
commands, conventions, style). A skill loads only when its one-line
description matches the task — use it for longer, self-contained workflows
(a release checklist, a research procedure, scripts to run), especially ones
that bundle reference files. Rules of thumb: if it must apply to *every* turn,
it belongs in `AGENTS.md`; if it's a multi-step procedure the agent needs only
sometimes, make it a skill and keep the always-paid cost to one index line.

**Memory vs AGENTS.md.** `AGENTS.md` is human-authored and versioned with the
repo; memory is agent-written and accumulates across sessions. Let memory hold
what emerges from working together — user preferences, feedback, decisions,
gotchas — and promote anything that hardens into a standing rule to
`AGENTS.md`. The injected memory policy draws the same line from the other
side: the agent is told not to save what the repo already records, `AGENTS.md`
included.

## Importing from Claude Code

marim's memory format mirrors Claude Code's, so an existing store carries over
directly:

    marim import claude              # dry run — reports, writes nothing
    marim import claude --apply      # perform it

Claude keeps memory per project directory, outside the repo, under
`$CLAUDE_CONFIG_DIR/projects/<path-slug>/memory` (default `~/.claude`). The
command derives that path from the workspace root. If it does not exist — a
worktree, or a project you opened from a different path — it lists the stores it
can see; pass one with `--from`:

    marim import claude --from ~/.claude/projects/-home-me-Projects-app

Memories land in **project scope**, `<workspace>/.marim/memory`, matching the
directory Claude keyed them to. If `.marim/` is not gitignored, `--apply` warns
that the imported memories would be committable.

A memory whose slug already exists in the target is skipped, as is one whose
title is already claimed by a different slug — either would overwrite something
marim's own `remember` tool wrote. `--force` overwrites both. Claude's extra
frontmatter keys (`originSessionId`, `modified`) are dropped; marim reads none
of them.

Project instruction files need no import: marim already reads a `CLAUDE.md` in
the workspace root when there is no `AGENTS.md`. Skills, sub-agents, hooks and
MCP servers are not imported yet.
