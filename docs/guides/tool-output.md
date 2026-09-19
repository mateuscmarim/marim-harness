# Large tool output

Marim uses Pydantic AI Harness's `ToolOutputLimits` for ordinary tool returns
in the native main agent and native sub-agents. This covers built-in tools,
custom tools, skill instructions and resources, and MCP results. External
The `claude-cli` agent loop manages their own tools and output.

## What the model receives

- Text shorter than **10,000 characters** passes through unchanged, including
  empty text. The threshold is inclusive: **10,000 characters or more** spills
  to the session's output store.
- A successful spill returns an upstream handle and a head-and-tail preview.
  The default preview budget is **1,000 characters**, plus the handle and
  retrieval instructions. The stored file contains the complete result that
  the producer returned. Structured results use indented JSON for line paging.
- If storage fails, upstream falls back to head-and-tail truncation at
  **4,000 characters**. The discarded content is unavailable through a handle.
- Reduction makes **no model requests**. It does not summarize results.

Tool-call IDs and upstream overflow metadata remain in persisted message
history; the model-facing result and streaming observers receive the preview.
Output reduction happens after execution and leaves tool approvals unchanged.

## Read a saved result

Each native agent has one read-only `read_tool_result` tool, including when
other tools use deferred loading. Pass the handle shown in the preview:

```python
read_tool_result(handle="<handle from preview>", offset=0, limit=200)
read_tool_result(handle="<handle from preview>", pattern="error", limit=50)
read_tool_result(handle="<handle from preview>", from_end=True, limit=20)
```

`offset` is the number of lines to skip, starting at zero; `limit` must be at
least one and is clamped upstream. `pattern` filters by a **literal substring**,
not a regular expression, before pagination. `from_end=True` counts from the
end. Read responses have their own line and character bounds.

Handles resolve only inside the owning output root. The retrieval tool cannot
read unrelated workspace files or targets outside that root through a symlink.
Its name is reserved: custom tools must not register `read_tool_result`.

## Storage and resume

With the [session scratchpad](skills-and-memory.md#the-session-scratchpad)
enabled, ordinary results live under `<scratchpad>/tool-results/`. With no
scratchpad, they live under
`<workspace>/.marim/output/upstream/<session-key>/`. A sessionless embedded
harness uses a stable ephemeral key for its lifetime.

Runs and detached jobs capture their storage destination before starting.
Switching sessions does not redirect output from work already in flight.
Resuming the same session can read its handles while the files remain present.
Saving the transcript does not copy spill files into the session JSON.

Scratchpad output shares the scratchpad's lifetime: session deletion or cleanup
of the system temp directory can remove it. Workspace fallback files persist
until removed. Changing the scratchpad setting or relocating the workspace can
change the output root, leaving earlier relative handles unavailable.

If a file is missing, `read_tool_result` returns an explanation. It never
reruns the original tool automatically. Preserve important results as ordinary
workspace files before temporary storage disappears.

Older sessions may contain absolute `saved to` pointers from Marim's previous
output format. Those pointers retain their existing load-time revalidation:
missing-file notes are appended without removing the preview; live files stay
readable through the original file tools. Explicit report budgets can also
still produce these absolute pointers.

## Bounds that still apply

TUI `!` shell commands collect at most **4,000 bytes** of output, keeping the
head and tail with an omission notice. Exit status and timeout markers are
added separately. Both the transcript and the next prompt receive this bounded
result. These user-run commands bypass the agent's output capability, so their
omitted output is not stored; redirect the command to a file to keep it in full.

Background-shell results shown by `/jobs output` or the HTTP job-detail endpoint
use a **20,000-character** head-and-tail preview plus an omission notice. The
registry retains the complete collected result for agent `job_output` and
`wait_for_job` calls, where Pydantic AI applies the ordinary spill policy.

Producer limits apply before output reduction: shell collection buffers,
network download limits, file-read pagination, image size limits, and bounded
search collection remain in place. A spill contains only what the producer
collected, including any collection-limit note, command exit status, or timeout
marker. It cannot recover output discarded before reduction.

Supported media results, including mixed text and media and `ToolReturn`-wrapped
media, pass through intact so image/audio bytes reach the model. These mixed
results are exempt from the general text threshold.

Explicit sub-agent `max_output_chars` budgets and the workflow final-result cap
remain at their report boundaries, including detached work. Ordinary tools used
inside native sub-agents still use the shared upstream output policy.
