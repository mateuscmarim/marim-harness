# Show relative paths for workspace files in tool display

## Problem

Tool cards in both the mobile app and TUI show full absolute paths (e.g.
`/home/mateuscmarim/Projects/marim.dev/marim-harness/src/...`) which get
truncated on narrow screens and are noisy everywhere.

## Solution

Strip the workspace root prefix from file paths at display time in both
clients, showing relative paths (e.g. `marim-harness/src/...`).

## Scope

- Harness TUI (`tool_summary.py` + `tools.py`)
- Mobile app (`ToolDisplay.kt` + ViewModel pipeline + composables)

## Design

### 1. Harness TUI

**`tool_summary.py`** — add `workspace_root` param to `summarize()` and
`_raw_target()`. After resolving the target for file tools, strip the
workspace prefix. Commands, patterns, URLs are unaffected.

**`tools.py`** — `ToolCallWidget._summary()` passes `self._workspace_root`
to `summarize()`.

### 2. Mobile app

**`ToolDisplay.kt`** — add `stripWorkspacePrefix(path, root)` utility. Update
`toolRow()` and `toolInput()` to accept optional `workspaceRoot` and strip
prefix for file-path categories (READ, EDIT, LIST).

**`TranscriptUiState`** — add `workspacePath: String?`.

**`TranscriptViewModel`** — inject `WorkspaceRepository`, look up
`WorkspaceEntity.path` for the current session's server/workspace, expose
in state.

**Composables** — thread `workspaceRoot` through `ToolCardView`,
`ToolGroupView`, `ToolGroupRow`, `ToolCardHeader`, `ToolExpandedDetail`.

### 3. Tests

- TUI: cases for `_raw_target()` with/without workspace root
- Mobile: cases for `stripWorkspacePrefix()` and `toolRow()` with workspace root
