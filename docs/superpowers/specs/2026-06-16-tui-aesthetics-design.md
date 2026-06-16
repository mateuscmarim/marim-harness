# marim-harness — TUI Aesthetics Refresh — Design Spec

*Date: 2026-06-16 · Status: approved design, pre-implementation*

## 1. Purpose

The TUI currently reads as a stock Textual app: it inherits Textual's default
theme and mixes ad-hoc glyphs (`[?]`, `›`, `⚠`, `•`, `▸`). The goal of this work
is a distinct, cohesive visual identity for the harness without changing any
behavior.

**Chosen direction:** *Refined minimal* — one restrained accent on a neutral dark
base, thin rules, consistent unicode glyphs, generous spacing. Calm and modern;
ages well.

**Non-goals:** no new features, no layout/flow changes, no behavioral changes.
This is purely presentation: palette, stylesheet, glyph language, banner.

## 2. Scope

### In scope
- Four custom Textual themes sharing one neutral dark base, differing only by
  accent: `marim-teal` (default), `marim-amber`, `marim-violet`, `marim-green`.
- Theme switching via the built-in command palette, a new `/theme` command, and
  persistence to config.
- Move inline `CSS` out of `app.py` into `tui/styles.tcss` (`CSS_PATH`), rewritten
  against theme variables so it recolors automatically.
- Unify the glyph language across all message/tool widgets.
- Tool-call left-rule treatment (replacing bracket glyph prefixes).
- Restyle the banner: muted block-ASCII seated above a thin accent rule.
- Status-bar refinement (same data, lighter weight).

### Out of scope (noted, not built)
- Animations / transitions.
- Light-mode variants.
- Per-widget user-configurable colors.
- Any new panels, commands, or keybindings beyond `/theme`.

## 3. Design

### 3.1 Theming architecture

Define four `textual.theme.Theme` objects in a new module `tui/themes.py`. All
four share the same base tokens (`background`, `surface`, `panel`, `foreground`,
plus `text-muted` via the theme's variables) and differ only in `primary` /
`accent`. A shared base ensures the app feels like one product with a swappable
accent, not four different apps.

Palette intent (exact hex chosen during implementation, validated for contrast
on a dark base):

| Theme          | Accent (`primary`)      |
|----------------|-------------------------|
| `marim-teal`   | muted teal/cyan (default)|
| `marim-amber`  | soft amber/gold         |
| `marim-violet` | muted violet            |
| `marim-green`  | desaturated green       |

Registration & selection in `HarnessApp.on_mount`:
- Register all four with `self.register_theme(...)`.
- Set `self.theme = <persisted choice or "marim-teal">`.

Three ways to switch, all converging on `self.theme = name`:
1. **Command palette** (`Ctrl+P` → "Change theme") — built in, free.
2. **`/theme [name]`** — added to the existing `commands.py` dispatch. No
   argument lists available themes and the current one; an argument sets it
   (validated against the registered names; unknown name → notice, no change).
3. **Persistence** — on change, write the theme name to the harness config so it
   survives restarts. Loaded back in `on_mount` to pick the startup theme.

### 3.2 Stylesheet cleanup

- Remove the inline `CSS = """..."""` string from `app.py`; add
  `CSS_PATH = "styles.tcss"` and create `tui/styles.tcss` with the same rules.
- Rewrite every rule to reference theme variables (`$primary`, `$surface`,
  `$panel`, `$text-muted`, `$error`) — **no hard-coded colors anywhere**, so a
  theme change recolors the whole app.
- Equivalent rules preserved for: `#log`, `PromptInput`, `#status-bar`,
  `#task-panel`, `#job-panel`, `.user-msg`, `.error-msg`, `.notice-msg`,
  `#banner`, `AssistantMessage`, `ToolCallWidget`, `SubAgentWidget`,
  `.subagent-body`.

### 3.3 Visual language

Unify the glyph set (single source of meaning per state):

| Element             | Before        | After |
|---------------------|---------------|-------|
| User prompt prefix  | `›`           | `›`   (kept) |
| Input cursor hint   | —             | `❯`   |
| Tool: pending       | `[?]`         | `·`   |
| Tool: done          | `[+]`         | `✓`   |
| Tool: denied        | `[x]`         | `✕`   |
| Sub-agent           | `[▸]`/`[+]`/`[x]` | `▸` / `✓` / `✕` |
| Notice              | `•`           | `·`   |
| Error               | `⚠`           | `✕`   |

Tool-call and sub-agent treatment:
- Replace the bracketed glyph prefix in `ToolCallWidget._summary` /
  `SubAgentWidget._summary` with the bare status glyph above.
- Add a left-rule to `ToolCallWidget` / `SubAgentWidget` via `border-left: tall
  $primary` (muted) in `styles.tcss`, giving the `│ ✓ read_file  app.py` look
  from the approved mockup. (`.subagent-body` already uses a left border; align
  it to the same token.)

Rules & spacing:
- Thin horizontal rule (`─`, `$text-muted`) under the banner and above the
  status bar. Implemented with a 1-cell `Static` rule or a `border-top`/`-bottom`
  on the adjacent widget — whichever is cleaner in `.tcss`.
- Consistent 1-cell vertical margins between message blocks (already mostly
  present; normalize stray values).

Status bar (`_status_text`): same fields (`name · mode · model · ctx · tokens ·
working…`), restyled lighter — muted separators, accent applied only to the
session name and to the context gauge once it crosses the existing 75% / 90%
thresholds (that conditional coloring already exists; keep it, drop accent
elsewhere).

### 3.4 Banner

Keep the existing MARIM block-ASCII (`_BANNER`), but:
- Recolor `#banner` from accent-bold to `$text-muted` (no bold).
- Tighten the `· · · a terminal harness` tagline spacing.
- Seat it above the §3.3 thin accent rule so it anchors rather than shouts.

## 4. Components touched

| File                     | Change |
|--------------------------|--------|
| `tui/themes.py` (new)    | Four `Theme` definitions + a registry/list helper. |
| `tui/styles.tcss` (new)  | Extracted + variable-driven stylesheet. |
| `tui/app.py`             | `CSS_PATH` instead of inline `CSS`; register themes + set startup theme in `on_mount`; recolor `#banner`; thin rules. |
| `tui/widgets.py`         | New glyph set in `ToolCallWidget` / `SubAgentWidget` / `UserMessage` / `ErrorMessage` / `NoticeMessage`; status-bar text weighting. |
| `tui/commands.py`        | `/theme [name]` command (list / set / validate). |
| `config.py`              | Read/write the persisted theme name. |

## 5. Data flow (theme switch)

```
user → /theme amber           command palette → "Change theme"
          │                              │
          ▼                              ▼
   commands.dispatch ─────────►  HarnessApp.theme = "marim-amber"
          │                              │
          ▼                              ▼
   config.set_theme("marim-amber")   Textual recomputes $primary/$accent
          │                              │
          ▼                              ▼
   persisted to disk            all widgets repaint from theme vars
```

## 6. Error handling

- `/theme <unknown>` → `NoticeMessage` listing valid names; no state change.
- Missing/invalid persisted theme on startup → fall back to `marim-teal` (never
  fatal).
- Config write failure on theme change → apply the theme in-session anyway,
  surface a notice; the unsaved choice simply won't persist.

## 7. Testing

- **Unit:** `themes.py` registry returns all four names; `/theme` parsing
  (list vs set vs invalid) via the existing command-dispatch tests; config
  round-trips the theme name.
- **Visual/manual:** launch the TUI, cycle all four themes via `/theme` and the
  command palette, confirm banner/tool-rule/status-bar render and that no
  hard-coded color survives a theme switch (everything recolors).
- No snapshot of behavior changes — this is presentation-only; existing
  behavioral tests must continue to pass unchanged.

## 8. YAGNI guardrails

Four dark themes + the cleanup, nothing more. No animation, no light mode, no
custom color editor, no new chrome. If a change doesn't serve the "stop looking
generic" goal, it's out.
