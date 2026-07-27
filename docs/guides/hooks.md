# Lifecycle hooks

Hooks are shell commands that marim runs at fixed points in a session's
lifecycle — session start and end, prompt submission, tool calls, compaction,
sub-agent spawns, and more. Each hook receives a JSON payload on stdin
describing the event. Most hooks are pure observers (log, notify, collect
metrics); two events can inject context into the model's prompt, and one can
block a manual `/compact`.

The engine is Claude-Code-compatible: event names, the config file shape, and
the stdin payload's common fields follow Claude Code's hook format, so many
Claude Code hook scripts work unchanged. Deviations are listed at the end.

> Provider note: under the `claude-cli` main-loop provider, marim's turn loop
> still runs, so session-level events (SessionStart, UserPromptSubmit, Stop,
> SessionEnd, …) fire normally — but Claude Code runs its own tools
> internally, so `PreToolUse`/`PostToolUse` never fire for those tool calls.

## Config files and precedence

Hooks are read from up to three sources and merged:

1. **Global** — `~/.config/marim/hooks.json` (strictly:
   `$XDG_CONFIG_HOME/marim/hooks.json`, falling back to `~/.config/marim/`
   when `XDG_CONFIG_HOME` is unset). Always loaded.
2. **Project** — `.marim/hooks.json` in the workspace root. Loaded **only
   when the project is trusted** (see below).
3. **Plugins** — hook entries from enabled *and trusted* plugins;
   project-scope plugins additionally require project trust.

Merging concatenates the per-event entry lists in that order (global first,
then project, then plugins) — nothing overrides anything; every matching hook
runs. A missing or malformed config file is skipped silently, never fatal.

### Config shape

The file is a JSON object with a top-level `hooks` key mapping an event name
to a list of entries. Each entry has an optional `matcher` and a `hooks` list
of command specs:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [
          {
            "type": "command",
            "command": "jq -r .tool_input.command >> ~/.marim-bash-log",
            "timeout": 10
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "cat .marim/context.md" }
        ]
      }
    ]
  }
}
```

Field semantics, as parsed by the engine:

- `type` — must be `"command"`; specs of any other type are skipped.
- `command` — a shell command line, run via the shell in its own process
  group. The payload arrives as JSON on stdin.
- `timeout` — seconds, per command spec; default 30. On timeout the whole
  process group is SIGKILLed. A non-numeric or non-positive value falls back
  to the default.
- `matcher` — an **anchored regex** (`re.fullmatch`, so `"Edit"` does not
  match `"MultiEdit"`). It applies only to four events: for `PreToolUse` /
  `PostToolUse` it matches the **tool name**; for `PreCompact` /
  `PostCompact` it matches the **trigger** (`manual` or `auto`). For every
  other event the matcher is ignored and the entry always runs. Absent,
  empty, or `"*"` matches everything; an invalid regex matches nothing.

Hooks are fail-soft by design: a crash, timeout, spawn failure, or nonzero
exit never breaks the turn — it is logged at DEBUG (`MARIM_DEBUG=1` to see
it) and the session continues. On observe-only events stderr is discarded.

## Trust: who gets to run project hooks

`.marim/hooks.json` ships with the repo, and hooks execute arbitrary
commands — so a cloned, untrusted repo must not be able to run code just
because you opened it in marim. Project-local hooks (and project-scope
plugin hooks) therefore load only when the project is trusted: a stored
per-project decision (the first-open prompt, `/trust on`, or `marim trust
grant`) or `MARIM_TRUST_PROJECT_HOOKS=1` (also `true`/`on`/`yes`) set in your
real shell environment or global config — a project's own `.env` is blocked
from setting the env var, so a repo cannot self-trust. Global hooks and
trusted non-project plugins always load. See
[guides/trust.md](trust.md) for the full rationale; the same gate governs
project MCP servers, skills, and agents.

## Events

Every payload carries four common fields:

```json
{
  "hook_event_name": "PreToolUse",
  "session_id": "…",
  "cwd": "/abs/path/to/workspace",
  "transcript_path": "/abs/path/to/session/store/file"
}
```

plus the event-specific fields below. `transcript_path` points at marim's own
session store file, not a Claude Code-format transcript.

### SessionStart — *injects context*

Fires when a session opens: at startup, on resume, and after `/clear` / a
new-session command. Extra field: `source` — one of `"startup"`, `"resume"`,
`"clear"`. Anything the hook writes to stdout (see "Injecting context") is
stashed and prepended to the **next turn's** prompt, once.

### UserPromptSubmit — *injects context*

Fires when the user submits a prompt, before it goes to the model. Extra
field: `prompt` (the full assembled prompt text, including any per-turn
context marim already prepended). Stdout is prepended to **this turn's**
prompt. It cannot block or rewrite the prompt.

### PreToolUse — observe-only

Fires when the model calls a tool, as the call streams. Extra fields:
`tool_name`, `tool_input` (the parsed argument dict; `{}` if the args failed
to parse). Matcher subject: the tool name. It **cannot** block, approve, or
modify the call — approval is marim's own permission system.

### PostToolUse — observe-only

Fires after a tool call succeeds. Extra fields: `tool_name`, `tool_input`
(correlated from the matching call by tool-call id), `tool_response` (the
result, stringified). Matcher subject: the tool name.

### PostToolUseFailure — observe-only

Fires instead of PostToolUse when a tool call errors or is retried. Extra
fields: `tool_name`, `tool_input`, `error` (the retry/error message shown to
the model).

### PreCompact — *can block manual compaction*

Fires before history compaction, while the transcript is still full (so a
hook can snapshot the conversation before it is summarized). Extra fields:
`trigger` (`"manual"` for `/compact`, `"auto"` for threshold/overflow-driven
compaction) and `custom_instructions` (the text after `/compact`, else `""`).
Matcher subject: the trigger.

This is the one event with a verdict. A hook blocks by either:

- exiting with code **2** — stderr becomes the reason shown to the user, or
- exiting 0 with `{"decision": "block", "reason": "…"}` on stdout.

A block verdict is honored **only for a manual `/compact`**. On auto (or
forced post-overflow) compaction the block is logged and ignored — a hook
must never be able to wedge a session into the hard context limit. Any other
exit code, a crash, or a timeout allows compaction. All matching hooks still
run even after one blocks; the first block's reason wins.

### PostCompact — observe-only

Fires after compaction completes. Extra fields: `trigger`,
`pre_compact_tokens`, `post_compact_tokens`, and `stage` (which pipeline
stages ran: `"micro"`, `"summary"`, or `"micro+summary"`). Matcher
subject: the trigger. Note the two token counts are measured differently
(provider-measured vs a char/4 estimate), so a small delta can reflect the
estimator.

### Stop — observe-only

Fires once a turn produces its final text, after the history is persisted.
No extra fields.

### SubagentStart / SubagentStop — observe-only

Fire when a sub-agent is spawned and when it returns. Extra fields:
`subagent_type` and `task` on both; SubagentStop adds `result` (the
sub-agent's report, or `"error: …"` on failure).

### Notification — observe-only

Fires when the agent needs the user's attention. Extra fields:
`notification_type` (`"approval_needed"` when gated tool calls await
approval, `"ask_user"` when the agent asks a question), `title`, `message`.
Useful for desktop notifications on an idle terminal.

### TaskCompleted — observe-only

Fires when an item on the agent's task checklist transitions to done. Extra
fields: `task_id`, `task_subject`, `task_description`.

### SessionEnd — observe-only

Fires on teardown (TUI exit, headless completion, server shutdown). Extra
field: `reason` (currently always `"exit"`).

## Injecting context

Only `SessionStart` and `UserPromptSubmit` read stdout; everywhere else
stdout is ignored. A hook must exit 0 for its output to count. The engine
accepts either shape:

- **Plain text** — non-JSON stdout is injected verbatim.
- **Structured** — a JSON object with
  `{"hookSpecificOutput": {"additionalContext": "…"}}` (Claude Code's shape)
  or a top-level `"additionalContext"`. Valid JSON without either field
  injects nothing — so a hook can emit structured status without polluting
  the prompt.

When several hooks inject, their contexts are joined with newlines and
prepended ahead of the user's typed text. The injected prefix is wrapped in
marim's `<turn-context>` envelope, so a resumed session can recover exactly
what the user typed; the system prompt is untouched, preserving prompt
caching.

## Claude Code compatibility

Carried over:

- Event names are Claude Code's exact strings, and the config shape
  (`hooks` → event → `[{matcher, hooks: [{type, command, timeout}]}]`) is
  Claude Code's.
- Payload common fields (`hook_event_name`, `session_id`, `cwd`,
  `transcript_path`) and per-event fields like `tool_name` / `tool_input` /
  `tool_response`, SessionStart's `source`, PreCompact's `trigger` +
  `custom_instructions`.
- Anchored (full-match) matcher regexes on tool names, and trigger-based
  matching for the compact events.
- The `hookSpecificOutput.additionalContext` injection shape, and plain-text
  stdout as context.
- Exit 2 + stderr-as-reason, and `{"decision": "block"}`, for PreCompact.

Deviations found in the engine code:

- **PreToolUse, PostToolUse, UserPromptSubmit, and Stop are observe-only.**
  There is no permission `decision` for tool calls, no prompt blocking, and
  no forced continuation — exit 2 has special meaning only on PreCompact.
- **PreCompact blocks only manual compaction**; Claude Code has no
  auto-compaction override carve-out because marim must protect the context
  limit.
- Default per-hook timeout is **30 seconds** (Claude Code documents 60).
- The matcher is ignored (entry always runs) for events other than the two
  tool events and the two compact events.
- Extra events beyond Claude Code's set: `PostCompact`, `SubagentStart`,
  `PostToolUseFailure`, and `TaskCompleted`.
- Only `type: "command"` hook specs are executed; anything else is skipped.
- `transcript_path` is marim's session store file, and payloads do not carry
  Claude Code extras like `permission_mode`.

## Worked examples

### 1. Inject project context at session start

Global or project config (`~/.config/marim/hooks.json`, or
`.marim/hooks.json` under trust):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "~/.config/marim/hooks/context.sh" }
        ]
      }
    ]
  }
}
```

`~/.config/marim/hooks/context.sh` (make it executable):

```sh
#!/bin/sh
# stdin: {"hook_event_name":"SessionStart","source":"startup",...}
source=$(jq -r .source)
echo "Session context (source: $source):"
echo "- Current branch: $(git branch --show-current 2>/dev/null || echo n/a)"
echo "- Open TODOs: $(grep -rc TODO src 2>/dev/null | wc -l) files"
```

Plain stdout on exit 0 is injected verbatim, prepended to the next turn's
prompt. To be explicit (and to stay silent when there is nothing to say),
emit the structured shape instead:

```sh
jq -n --arg ctx "Branch: $(git branch --show-current)" \
  '{hookSpecificOutput: {additionalContext: $ctx}}'
```

### 2. Log every tool call

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "jq -c '{t: now | todate, tool: .tool_name, input: .tool_input}' >> ~/.local/state/marim-tools.jsonl",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Each successful tool call appends one JSON line. Narrow it with the matcher —
`"matcher": "bash"` logs only shell commands, `"matcher": "edit_file|write_file"`
only file mutations. Add the same command spec under `PostToolUseFailure` to
capture failed calls too (they do not fire PostToolUse). The hook's own
stdout is ignored for these events, so the redirect is where the data goes.
