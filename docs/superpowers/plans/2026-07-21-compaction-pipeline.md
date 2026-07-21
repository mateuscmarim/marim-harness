# Compaction Pipeline Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework compaction into a staged mask-then-summarize pipeline with a manual `/compact` command, PreCompact blocking (manual only) + PostCompact hooks, a rapid-refill thrash breaker, and scratchpad-persisted elided payloads.

**Architecture:** All pure logic (breaker, masking, planning, prompts) stays in `compaction.py`; effectful orchestration stays in `SessionController.maybe_compact` (`session/ctrl.py`); the hook engine gains one new verdict-returning dispatch path used only by PreCompact. Spec: `docs/superpowers/specs/2026-07-21-compaction-pipeline-design.md`.

**Tech Stack:** Python ≥3.10, pydantic-ai message types, pytest (asyncio via anyio markers already in the suite), ruff (C901 ≤ 10), pyright.

**One deviation from the spec, decided here:** the spec proposed `pre_compact`/`post_compact` wrappers on `hooks/dispatch.py`'s `TurnHooks`. `SessionController` already calls `self.deps.hooks.dispatch(...)` directly for PRE_COMPACT (it never goes through `TurnHooks`), so wrappers there would be dead code. The plan keeps ctrl's direct-call style. Everything else follows the spec.

## Global Constraints

- Python floor is 3.10 — no 3.11+ syntax.
- `uv run …` for everything; never bare `python`/`pytest`/`pip`.
- Ruff line length 100; cyclomatic complexity cap 10 (`C901`) — extract helpers rather than `# noqa`.
- Preserve the long "why" comments around resumability and caching; add equivalents for new invariants.
- Tool-call/return pairing must never break: masking only replaces `ToolReturnPart.content`; tail cuts only at user-turn boundaries.
- Hook subprocess failures are swallowed and logged — a crashing hook is never a block.
- CI order before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.

---

### Task 1: `CompactionBreaker` (pure)

**Files:**
- Modify: `src/marim_harness/compaction.py` (append after `last_request_input_tokens`, ~line 91)
- Test: `tests/test_compaction.py` (append)

**Interfaces:**
- Produces: `CompactionBreaker` dataclass with `open: bool` property, `note_turn()`, `note_compact()`, `reset()`; module constant `BREAKER_NOTICE: str`. Consumed by Task 6.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_compaction.py` (import `CompactionBreaker` in the existing `from marim_harness.compaction import (...)` block):

```python
def test_breaker_trips_after_three_rapid_refills():
    b = CompactionBreaker()
    b.note_compact()                    # first compaction: baseline, not rapid
    for _ in range(3):                  # three refill-compactions within 3 turns each
        b.note_turn()
        b.note_compact()
    assert b.open


def test_breaker_slow_refill_resets_the_streak():
    b = CompactionBreaker()
    b.note_compact()
    b.note_turn(); b.note_compact()     # rapid #1
    b.note_turn(); b.note_compact()     # rapid #2
    for _ in range(4):                  # 4 turns > rapid_turns → streak broken
        b.note_turn()
    b.note_compact()
    assert not b.open
    assert b.consecutive_rapid_refills == 0


def test_breaker_reset_clears_everything():
    b = CompactionBreaker()
    b.note_compact()
    for _ in range(3):
        b.note_turn(); b.note_compact()
    assert b.open
    b.reset()
    assert not b.open
    assert b.turns_since_compact is None


def test_breaker_ignores_turns_before_first_compact():
    b = CompactionBreaker()
    for _ in range(10):
        b.note_turn()
    b.note_compact()
    assert b.consecutive_rapid_refills == 0
    assert not b.open
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_compaction.py -k breaker -v`
Expected: FAIL — `ImportError: cannot import name 'CompactionBreaker'`

- [ ] **Step 3: Implement**

Append to `src/marim_harness/compaction.py` after `last_request_input_tokens`:

```python
# Shown once when the breaker opens; mirrors Claude Code's thrashing message.
BREAKER_NOTICE = (
    "Auto-compaction is thrashing: the context refilled right after each of the "
    "last 3 compactions. A file read or tool output is likely too large for the "
    "context window — read in smaller chunks, or /clear to start fresh."
)


@dataclasses.dataclass
class CompactionBreaker:
    """Rapid-refill circuit breaker for auto-compaction.

    If a compaction's result refills past the threshold within ``rapid_turns``
    turns, ``trip_after`` consecutive times, the breaker opens and the caller
    should skip *auto* compaction (manual and forced compaction bypass it).
    Without this, one oversized tool observation re-triggers the summarizer
    every turn forever — burning summarizer calls without ever getting under
    the threshold. Pure state machine: the owner calls ``note_turn()`` once per
    post-turn compaction check and ``note_compact()`` when a compaction fires.
    """

    rapid_turns: int = 3
    trip_after: int = 3
    turns_since_compact: int | None = None  # None until the first compaction
    consecutive_rapid_refills: int = 0

    @property
    def open(self) -> bool:
        return self.consecutive_rapid_refills >= self.trip_after

    def note_turn(self) -> None:
        if self.turns_since_compact is not None:
            self.turns_since_compact += 1

    def note_compact(self) -> None:
        if (
            self.turns_since_compact is not None
            and self.turns_since_compact <= self.rapid_turns
        ):
            self.consecutive_rapid_refills += 1
        else:
            self.consecutive_rapid_refills = 0
        self.turns_since_compact = 0

    def reset(self) -> None:
        self.turns_since_compact = None
        self.consecutive_rapid_refills = 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_compaction.py -k breaker -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/compaction.py tests/test_compaction.py
git commit -m "feat(compaction): rapid-refill circuit breaker (pure state machine)"
```

---

### Task 2: Mask-with-persist — pointer placeholders

**Files:**
- Modify: `src/marim_harness/compaction.py` (`MASKED_OBSERVATION` block, ~line 200, and `mask_stale_observations`, ~line 208)
- Test: `tests/test_compaction.py` (append; `_tool_return` helper already exists at ~line 326)

**Interfaces:**
- Consumes: nothing new.
- Produces: `mask_stale_observations(history, keep_recent=4, *, min_chars=200, persist=None)` where `persist: Callable[[str, str], str | None] | None` takes `(content, tool_name)` and returns a path string or `None`; constants `ELIDED_POINTER_PREFIX`, helper `_is_masked(content) -> bool`. Consumed by Tasks 3 and 6.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_compaction.py`:

```python
def test_mask_persist_puts_path_in_placeholder():
    history = [_tool_return(f"t{i}", "X" * 300) for i in range(6)]
    calls: list[tuple[str, str]] = []

    def persist(content: str, tool_name: str) -> str:
        calls.append((content, tool_name))
        return f"/pad/elided/{len(calls):03d}-{tool_name}.txt"

    masked, n = mask_stale_observations(history, keep_recent=2, persist=persist)
    assert n == 4 and len(calls) == 4
    first = masked[0].parts[0]
    assert first.content.startswith(ELIDED_POINTER_PREFIX)
    assert "/pad/elided/001-" in first.content
    assert "read_file" in first.content
    # persisted content is the original payload
    assert calls[0][0] == "X" * 300


def test_mask_persist_failure_falls_back_to_plain_placeholder():
    history = [_tool_return(f"t{i}", "X" * 300) for i in range(3)]
    masked, n = mask_stale_observations(
        history, keep_recent=1, persist=lambda content, name: None
    )
    assert n == 2
    assert masked[0].parts[0].content == MASKED_OBSERVATION


def test_mask_is_idempotent_over_pointer_placeholders():
    history = [_tool_return(f"t{i}", "X" * 300) for i in range(4)]
    once, n1 = mask_stale_observations(
        history, keep_recent=1, persist=lambda c, t: "/pad/e/001-x.txt"
    )
    twice, n2 = mask_stale_observations(
        once, keep_recent=1, persist=lambda c, t: "/pad/e/002-x.txt"
    )
    assert n1 == 3 and n2 == 0
    assert [p.parts[0].content for p in twice] == [p.parts[0].content for p in once]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_compaction.py -k "persist or pointer" -v`
Expected: FAIL — `ELIDED_POINTER_PREFIX` not importable / unexpected `persist` kwarg

- [ ] **Step 3: Implement**

In `src/marim_harness/compaction.py`, after the `MASKED_OBSERVATION` constant add:

```python
# When the payload was persisted to the session scratchpad before eliding, the
# placeholder points at the file so the model can recover the exact bytes with
# read_file instead of re-running the tool. Both placeholder forms are treated
# as already-masked by _is_masked, keeping re-runs idempotent.
ELIDED_POINTER_PREFIX = "[output elided to save context; full content at "


def _elided_pointer(path: str) -> str:
    return f"{ELIDED_POINTER_PREFIX}{path} — read_file it if still needed]"


def _is_masked(content) -> bool:
    return content == MASKED_OBSERVATION or (
        isinstance(content, str) and content.startswith(ELIDED_POINTER_PREFIX)
    )
```

Change `mask_stale_observations`'s signature and the two relevant lines in its loop:

```python
def mask_stale_observations(
    history: list,
    keep_recent: int = 4,
    *,
    min_chars: int = 200,
    persist: Callable[[str, str], str | None] | None = None,
) -> tuple[list, int]:
```

Extend its docstring with: `persist`, when given, is called with `(content, tool_name)` and should write the payload somewhere recoverable, returning the path (used in the placeholder) or `None` (plain placeholder). Persist is best-effort: a `None`/failure never blocks masking.

In the loop, replace the `if seen <= keep_recent or part.content == MASKED_OBSERVATION:` check with:

```python
            if seen <= keep_recent or _is_masked(part.content):
                continue
```

and replace the `new_parts[pidx] = dataclasses.replace(...)` line with:

```python
            replacement = MASKED_OBSERVATION
            if persist is not None:
                path = persist(str(part.content), part.tool_name)
                if path:
                    replacement = _elided_pointer(path)
            new_parts[pidx] = dataclasses.replace(part, content=replacement)
```

- [ ] **Step 4: Run the whole compaction test file**

Run: `uv run pytest --no-cov tests/test_compaction.py -v`
Expected: all PASS (including the four pre-existing mask tests — no behavior change without `persist`)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/compaction.py tests/test_compaction.py
git commit -m "feat(compaction): mask placeholders can point at persisted payloads"
```

---

### Task 3: `persist_elided` scratchpad helper

**Files:**
- Modify: `src/marim_harness/workspace/scratchpad.py`
- Test: `tests/test_scratchpad.py` (append)

**Interfaces:**
- Produces: `persist_elided(scratchpad: Path, content: str, hint: str) -> Path | None`. Writes `<scratchpad>/elided/NNN-<slug>.txt`. Consumed by Task 6.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scratchpad.py`:

```python
from marim_harness.workspace.scratchpad import persist_elided


def test_persist_elided_writes_numbered_slugged_file(tmp_path):
    p1 = persist_elided(tmp_path, "payload one", "run_bash")
    p2 = persist_elided(tmp_path, "payload two", "read_file")
    assert p1 is not None and p1.name == "001-run_bash.txt"
    assert p2 is not None and p2.name == "002-read_file.txt"
    assert p1.parent == tmp_path / "elided"
    assert p1.read_text(encoding="utf-8") == "payload one"


def test_persist_elided_sanitizes_hint(tmp_path):
    p = persist_elided(tmp_path, "x", "mcp__weird/Tool Name!")
    assert p is not None
    assert p.name == "001-mcp__weird-tool-name.txt"


def test_persist_elided_failure_returns_none(tmp_path):
    blocker = tmp_path / "elided"
    blocker.write_text("not a directory")   # occupies the dir name with a file
    assert persist_elided(tmp_path, "x", "run_bash") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_scratchpad.py -k elided -v`
Expected: FAIL — `ImportError: cannot import name 'persist_elided'`

- [ ] **Step 3: Implement**

Add `import re` to `scratchpad.py`'s imports, then append:

```python
def persist_elided(scratchpad: Path, content: str, hint: str) -> Path | None:
    """Write an elided tool payload under ``<scratchpad>/elided/`` and return
    its path, or None on any failure (callers degrade to a plain placeholder).

    Files are numbered so listing the directory reads chronologically; the
    ``hint`` (tool name) is slugged into the filename so the model can spot
    the right one without opening each. Best-effort by contract: compaction
    must proceed identically whether this succeeds or not.
    """
    try:
        d = scratchpad / "elided"
        d.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9_-]+", "-", hint.lower()).strip("-") or "output"
        n = sum(1 for _ in d.glob("*.txt")) + 1
        path = d / f"{n:03d}-{slug[:40]}.txt"
        path.write_text(content, encoding="utf-8")
        return path
    except OSError as exc:
        logger.debug("persist_elided failed: %s", exc)
        return None
```

Note: `test_persist_elided_sanitizes_hint` runs against a `tmp_path` whose `elided/` dir may already hold files from earlier asserts in the same test — the slug assertion is the meaningful one; keep the numbering assertion loose as written.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_scratchpad.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/workspace/scratchpad.py tests/test_scratchpad.py
git commit -m "feat(scratchpad): persist_elided writes recoverable elided payloads"
```

---

### Task 4: Structured summarizer + custom instructions

**Files:**
- Modify: `src/marim_harness/compaction.py` (`Summarizer` type ~line 41, `_SUMMARY_INSTRUCTIONS` ~line 359, `_summarize_prompt` ~line 377, `make_summarizer` ~line 394, `compact_history_with_summary` ~line 318)
- Test: `tests/test_compaction.py` (append + adjust `_summarizer` helper at ~line 206)

**Interfaces:**
- Consumes: nothing new.
- Produces: `Summarizer = Callable[[list[ModelMessage], str | None], Awaitable[str]]`; `compact_history_with_summary(..., instructions: str | None = None)`; `_summarize_prompt(transcript, instructions=None)`. Consumed by Task 6. **Breaking protocol change** for embedders' custom summarizers — called out in the commit message.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_compaction.py`:

```python
def test_summarize_prompt_appends_compact_instructions_block():
    prompt = _summarize_prompt("T", "focus on the auth bug")
    assert "## Compact instructions" in prompt
    assert "focus on the auth bug" in prompt
    assert "## Compact instructions" not in _summarize_prompt("T", None)


def test_summary_instructions_cover_the_structured_schema():
    for needle in (
        "Primary request and intent",
        "All user messages",
        "verbatim",
        "Next step",
        "Security-relevant",
    ):
        assert needle in _SUMMARY_INSTRUCTIONS, needle


async def test_compact_with_summary_threads_instructions_to_summarizer():
    received: list = []

    async def summarizer(messages, instructions=None):
        received.append(instructions)
        return "SUMMARY"

    history = _history(rounds=12)
    await compact_history_with_summary(
        history, max_tokens=10, summarizer=summarizer, instructions="keep the tests"
    )
    assert received == ["keep the tests"]
```

Also update the existing `_summarizer` factory (~line 206) so its inner function accepts the new second argument — change its signature to `async def summarize(messages, instructions=None):` (body unchanged). Add `_summarize_prompt` and `_SUMMARY_INSTRUCTIONS` to the test file's import block.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_compaction.py -k "instructions or schema" -v`
Expected: FAIL — no `## Compact instructions`, schema needles missing, unexpected kwarg

- [ ] **Step 3: Implement**

Replace the `Summarizer` alias (~line 41):

```python
# (messages, custom_instructions) -> summary text. Instructions come from a
# manual `/compact <instructions>` and are None for automatic compaction.
Summarizer = Callable[[list[ModelMessage], str | None], Awaitable[str]]
```

Replace `_SUMMARY_INSTRUCTIONS`:

```python
_SUMMARY_INSTRUCTIONS = (
    "You compress a coding-session transcript into a dense summary so the agent "
    "can keep working with less context. Write terse notes, not prose, under "
    "these headings:\n"
    "1. Primary request and intent — every explicit ask from the user.\n"
    "2. Key technical concepts — technologies, patterns, decisions.\n"
    "3. Files and code sections — files read or edited, what changed and why, "
    "with short snippets only where essential to continue.\n"
    "4. Errors and fixes — each error hit, how it was fixed, and any user "
    "feedback about doing it differently.\n"
    "5. All user messages — every non-tool-result user message, condensed but "
    "none omitted.\n"
    "6. Pending tasks — work explicitly requested but not finished.\n"
    "7. Current work — precisely what was in progress at the cut, file names "
    "and snippets included.\n"
    "8. Next step — only if directly in line with the most recent explicit "
    "request; include a verbatim quote from the recent conversation showing "
    "where work left off, so the task cannot drift.\n"
    "Security-relevant user instructions (files or data to avoid, operations "
    "that must not be performed, credential handling rules) MUST be preserved "
    "verbatim so they continue to apply after compaction. Drop pleasantries "
    "and redundant detail."
)
```

Replace `_summarize_prompt`:

```python
def _summarize_prompt(transcript: str, instructions: str | None = None) -> str:
    """Wrap the transcript in an explicit, in-message summarize instruction. A bare
    transcript with the rules only in the system prompt lets weaker models reply
    conversationally instead of summarizing; restating the task in the user turn
    and delimiting the transcript keeps them on task. ``instructions`` is the
    user's manual `/compact` focus, honored as an extra block the summarizer is
    told to follow."""
    extra = (
        f"\n\n## Compact instructions\nAlso follow these user-supplied "
        f"instructions when summarizing:\n{instructions}\n"
        if instructions
        else ""
    )
    return (
        "Summarize the coding-session transcript below into dense notes under "
        "the headings from your instructions. Output only the summary — do not "
        "reply conversationally or address the user."
        f"{extra}\n\n"
        "=== TRANSCRIPT START ===\n"
        f"{transcript}\n"
        "=== TRANSCRIPT END ===\n\n"
        "Summary:"
    )
```

Update `make_summarizer`'s inner function:

```python
    async def summarize(messages: list, instructions: str | None = None) -> str:
        result = await summary_agent.run(
            _summarize_prompt(render_transcript(messages), instructions)
        )
        return result.output
```

In `compact_history_with_summary`: add `instructions: str | None = None` to the keyword-only params and change the call to `summary = await summarizer(middle, instructions)`.

- [ ] **Step 4: Run the file's tests**

Run: `uv run pytest --no-cov tests/test_compaction.py -v`
Expected: all PASS. If `test_make_summarizer_sends_framed_prompt_to_model` asserts on old prompt wording, update its needles to the new phrasing ("dense notes under the headings", `=== TRANSCRIPT START ===`).

- [ ] **Step 5: Check for other summarizer call sites**

Run: `grep -rn "summarizer(" src/marim_harness tests --include="*.py" | grep -v "make_summarizer\|self.summarizer is\|test_compaction"`
Fix any caller passing a single argument to a `Summarizer` (e.g. stub summarizers in `tests/test_session.py`: give each `instructions=None` as a second parameter). `grep -rn "async def.*summar" tests/` finds them.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/compaction.py tests/test_compaction.py tests/test_session.py
git commit -m "feat(compaction): structured summary schema + manual compact instructions

BREAKING: the Summarizer protocol now takes (messages, instructions)."
```

---

### Task 5: Hook engine — verdicts and PostCompact

**Files:**
- Modify: `src/marim_harness/hooks/events.py`, `src/marim_harness/hooks/runner.py`
- Test: `tests/test_hooks_events.py`, `tests/test_hooks_runner.py` (append)

**Interfaces:**
- Produces: `events.POST_COMPACT = "PostCompact"`; `HookVerdict(blocked: bool, reason: str)` frozen dataclass; `HookRunner.dispatch_verdict(event, payload) -> HookVerdict` (matcher subject = `payload["trigger"]`). Consumed by Task 6.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hooks_events.py`:

```python
def test_post_compact_event_name():
    assert events.POST_COMPACT == "PostCompact"
```

Append to `tests/test_hooks_runner.py` (follows the file's existing pattern of writing tiny scripts into `tmp_path`; reuse its helper for making executables if one exists, else `Path.write_text` + `chmod`):

```python
from marim_harness.hooks.runner import HookVerdict


def _script(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


async def test_verdict_exit_2_blocks_with_stderr_reason(tmp_path):
    cmd = _script(tmp_path, "block.sh", 'echo "dirty git state" >&2; exit 2')
    runner = HookRunner({events.PRE_COMPACT: [{"hooks": [{"type": "command", "command": cmd}]}]})
    v = await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "manual"})
    assert v.blocked and "dirty git state" in v.reason


async def test_verdict_json_decision_block(tmp_path):
    cmd = _script(tmp_path, "jb.sh", """echo '{"decision": "block", "reason": "nope"}'""")
    runner = HookRunner({events.PRE_COMPACT: [{"hooks": [{"type": "command", "command": cmd}]}]})
    v = await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "manual"})
    assert v.blocked and v.reason == "nope"


async def test_verdict_clean_exit_and_malformed_json_do_not_block(tmp_path):
    for body in ("exit 0", "echo not-json", "echo '{\"decision\": \"allow\"}'"):
        cmd = _script(tmp_path, f"ok{hash(body) & 0xFF}.sh", body)
        runner = HookRunner(
            {events.PRE_COMPACT: [{"hooks": [{"type": "command", "command": cmd}]}]}
        )
        v = await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "auto"})
        assert not v.blocked


async def test_verdict_crash_and_other_exit_codes_are_not_blocks(tmp_path):
    cmd = _script(tmp_path, "crash.sh", "exit 1")
    runner = HookRunner({events.PRE_COMPACT: [{"hooks": [{"type": "command", "command": cmd}]}]})
    v = await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "manual"})
    assert not v.blocked
    missing = HookRunner(
        {events.PRE_COMPACT: [{"hooks": [{"type": "command", "command": "/nonexistent/hook"}]}]}
    )
    assert not (await missing.dispatch_verdict(events.PRE_COMPACT, {"trigger": "manual"})).blocked


async def test_verdict_matcher_matches_trigger(tmp_path):
    cmd = _script(tmp_path, "m.sh", "exit 2")
    runner = HookRunner(
        {events.PRE_COMPACT: [{"matcher": "manual", "hooks": [{"type": "command", "command": cmd}]}]}
    )
    assert (await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "manual"})).blocked
    assert not (await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "auto"})).blocked


async def test_verdict_unconfigured_event_allows():
    assert not (await HookRunner({}).dispatch_verdict(events.PRE_COMPACT, {})).blocked
```

If `tests/test_hooks_runner.py` already defines a script-writing helper, use it instead of adding `_script` (check its lines 10–32 first).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_hooks_runner.py -k verdict -v tests/test_hooks_events.py`
Expected: FAIL — `HookVerdict` not importable

- [ ] **Step 3: Implement**

`events.py`: after `PRE_COMPACT` add `POST_COMPACT = "PostCompact"`.

`runner.py`: add near the top:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class HookVerdict:
    """Outcome of a verdict dispatch (PreCompact only). ``blocked`` is honored
    by the caller only for manual triggers; a crash, timeout, or nonzero exit
    other than 2 is never a block (the swallow-and-log contract holds)."""

    blocked: bool = False
    reason: str = ""
```

Add the verdict runner (a sibling of `_run_one` — it captures stderr and interprets exit codes instead of harvesting stdout context):

```python
async def _run_one_verdict(command: str, payload: dict, timeout) -> HookVerdict:
    """Run one hook for a verdict. Exit 2 blocks (stderr = reason); exit 0 with
    ``{"decision": "block"}`` on stdout blocks; everything else — including
    spawn failure, timeout, and other exit codes — allows."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        logger.debug("hook command %r failed to spawn: %s", command, exc)
        return HookVerdict()
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(payload).encode()),
            timeout=_coerce_timeout(timeout),
        )
    except (asyncio.TimeoutError, OSError, ValueError) as exc:
        logger.debug("hook command %r failed/timed out: %s", command, exc)
        _kill(proc)
        await proc.wait()
        return HookVerdict()
    if proc.returncode == 2:
        return HookVerdict(blocked=True, reason=stderr.decode(errors="replace").strip())
    if proc.returncode != 0:
        logger.debug("hook command %r exited %s", command, proc.returncode)
        return HookVerdict()
    out = stdout.decode(errors="replace").strip()
    if out:
        try:
            data = json.loads(out)
        except ValueError:
            return HookVerdict()
        if isinstance(data, dict) and data.get("decision") == "block":
            return HookVerdict(blocked=True, reason=str(data.get("reason", "")))
    return HookVerdict()
```

Add the dispatch method to `HookRunner`:

```python
    async def dispatch_verdict(self, event: str, payload: dict) -> HookVerdict:
        """Run ``event``'s hooks for a block/allow verdict. The matcher subject
        is the payload's ``trigger`` (Claude Code matches PreCompact hooks on
        "manual"/"auto", not on a tool name). All matching hooks run; the first
        block wins but later hooks still execute (observability). Never raises."""
        entries = self._config.get(event)
        if not entries:
            return HookVerdict()
        trigger = str(payload.get("trigger", ""))
        verdict = HookVerdict()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not _matches(entry.get("matcher"), event, trigger):
                continue
            for spec in entry.get("hooks", []) or []:
                if not isinstance(spec, dict) or spec.get("type") != "command":
                    continue
                command = spec.get("command")
                if not command:
                    continue
                try:
                    v = await _run_one_verdict(
                        str(command), payload, spec.get("timeout", _DEFAULT_TIMEOUT)
                    )
                except Exception as exc:
                    logger.warning("hook %r failed: %s", command, exc)
                    continue
                if v.blocked and not verdict.blocked:
                    verdict = v
        return verdict
```

- [ ] **Step 4: Run the hooks tests**

Run: `uv run pytest --no-cov tests/test_hooks_runner.py tests/test_hooks_events.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/hooks/events.py src/marim_harness/hooks/runner.py \
        tests/test_hooks_events.py tests/test_hooks_runner.py
git commit -m "feat(hooks): PreCompact block verdicts + PostCompact event"
```

---

### Task 6: The staged pipeline in `SessionController.maybe_compact`

**Files:**
- Modify: `src/marim_harness/session/ctrl.py` (`__init__` ~line 129, `_load_active_store` ~line 332, `maybe_compact` ~line 425)
- Modify: `src/marim_harness/runtime/harness.py` (`bind_ui` ~line 482)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`bind_ui` call ~line 114, callbacks near `_on_compact` ~line 652)
- Test: `tests/test_session.py` (update + append)

**Interfaces:**
- Consumes: `CompactionBreaker`, `BREAKER_NOTICE` (Task 1); `mask_stale_observations(..., persist=)` (Task 2); `persist_elided` (Task 3); `compact_history_with_summary(..., instructions=)` (Task 4); `dispatch_verdict`, `HookVerdict`, `events.POST_COMPACT` (Task 5); existing `Deps.get_scratchpad`.
- Produces: `maybe_compact(*, force: bool = False, trigger: str = "auto", instructions: str | None = None) -> bool`; `SessionController.breaker`; `SessionController.on_notice: Callable[[str], None] | None`; `bind_ui(..., on_notice=...)`. Consumed by Task 7.

- [ ] **Step 1: Update the existing recording fake, then write the failing tests**

First: the `_RecordingHooks` class inside `test_pre_compact_fires_before_compaction_work` (~line 587) only implements `dispatch`; the new pipeline calls `dispatch_verdict` for PreCompact, so extend it:

```python
    class _RecordingHooks:
        async def dispatch(self, event, payload):
            order.append(f"hook:{event}")

        async def dispatch_verdict(self, event, payload):
            order.append(f"hook:{event}")
            return HookVerdict()
```

Its assertion `order == [f"hook:{hook_events.PRE_COMPACT}", "summarizer"]` becomes `order[0] == f"hook:{hook_events.PRE_COMPACT}"` followed by `assert "summarizer" in order` and `assert order.index("summarizer") > 0` — PostCompact now also lands in `order` after the summarizer.

Then append (imports: `HookVerdict` from `marim_harness.hooks.runner`, `ELIDED_POINTER_PREFIX`, `estimate_tokens` from `marim_harness.compaction`, `ModelResponse`, `ToolCallPart`, `ToolReturnPart` from `pydantic_ai.messages`):

```python
def _bulky_tool_history(rounds: int = 8, payload: int = 6000) -> list:
    """User turns each followed by a tool round with a huge return — the shape
    where stage-1 masking alone can get a history back under threshold."""
    msgs = [ModelRequest(parts=[UserPromptPart(content="start")])]
    for i in range(rounds):
        msgs.append(ModelRequest(parts=[UserPromptPart(content=f"turn {i}")]))
        msgs.append(ModelResponse(parts=[
            ToolCallPart(tool_name="run_bash", args={"c": i}, tool_call_id=f"t{i}")
        ]))
        msgs.append(ModelRequest(parts=[
            ToolReturnPart(tool_name="run_bash", content="X" * payload, tool_call_id=f"t{i}")
        ]))
    msgs.append(ModelRequest(parts=[UserPromptPart(content="latest")]))
    return msgs


class _FakeHooks:
    """Records every dispatch and returns a fixed verdict for PreCompact."""

    def __init__(self, blocked: bool = False, reason: str = ""):
        self.events: list[str] = []
        self.payloads: dict[str, dict] = {}
        self._verdict = HookVerdict(blocked=blocked, reason=reason)

    async def dispatch(self, event, payload):
        self.events.append(f"dispatch:{event}")
        self.payloads[event] = payload

    async def dispatch_verdict(self, event, payload):
        self.events.append(f"verdict:{event}")
        self.payloads[event] = payload
        return self._verdict


@pytest.mark.anyio
async def test_stage1_masking_alone_skips_the_summarizer(tmp_path):
    """Over threshold, but the bloat is old tool output: masking must get the
    history under threshold WITHOUT a summarizer call."""
    called = []

    async def summarizer(messages, instructions=None):
        called.append(1)
        return "SUMMARY"

    deps = _make_deps(tmp_path, mode=Mode.ask)
    # 8 x 6000-char returns ≈ 12k tokens; masking all but the 4 most recent
    # drops ≈ 6k, landing under the 8k threshold.
    ctrl = SessionController(
        None, None, deps, max_context_tokens=8000, keep_last_messages=20,
        summarizer=summarizer, mask_observations=True,
    )
    ctrl.history = _bulky_tool_history()
    assert await ctrl.maybe_compact() is True
    assert called == []                                    # stage 1 sufficed
    assert estimate_tokens(ctrl.history) <= 8000


@pytest.mark.anyio
async def test_manual_bypasses_gate_and_resets_breaker(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.ask)
    ctrl = SessionController(
        None, None, deps, max_context_tokens=10_000_000, keep_last_messages=1,
    )
    ctrl.history = _bulky_tool_history()
    ctrl.breaker.consecutive_rapid_refills = 99
    assert await ctrl.maybe_compact(trigger="manual") is True   # under threshold, still compacts
    assert ctrl.breaker.consecutive_rapid_refills == 0          # reset, then non-rapid note


@pytest.mark.anyio
async def test_open_breaker_skips_auto_but_not_manual(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.ask)
    ctrl = SessionController(
        None, None, deps, max_context_tokens=10, keep_last_messages=1,
    )
    ctrl.history = _bulky_tool_history()
    notices: list[str] = []
    ctrl.on_notice = notices.append
    ctrl.breaker.consecutive_rapid_refills = ctrl.breaker.trip_after
    assert await ctrl.maybe_compact() is False
    assert len(notices) == 1                               # notice shown once
    assert await ctrl.maybe_compact() is False
    assert len(notices) == 1                               # ...and only once
    assert await ctrl.maybe_compact(trigger="manual") is True


@pytest.mark.anyio
async def test_manual_block_verdict_aborts_with_notice(tmp_path):
    hooks = _FakeHooks(blocked=True, reason="snapshot first")
    deps = _make_deps(tmp_path, mode=Mode.ask, hooks=hooks)
    ctrl = SessionController(
        None, None, deps, max_context_tokens=10, keep_last_messages=1,
    )
    ctrl.history = _bulky_tool_history()
    notices: list[str] = []
    ctrl.on_notice = notices.append
    before = list(ctrl.history)
    assert await ctrl.maybe_compact(trigger="manual") is False
    assert ctrl.history == before
    assert notices and "snapshot first" in notices[0]


@pytest.mark.anyio
async def test_auto_ignores_block_verdict(tmp_path):
    hooks = _FakeHooks(blocked=True, reason="nope")
    deps = _make_deps(tmp_path, mode=Mode.ask, hooks=hooks)
    ctrl = SessionController(
        None, None, deps, max_context_tokens=10, keep_last_messages=1,
    )
    ctrl.history = _bulky_tool_history()
    assert await ctrl.maybe_compact() is True              # block logged, not honored


@pytest.mark.anyio
async def test_pipeline_order_and_post_compact_payload(tmp_path):
    """Full pipeline: PreCompact verdict → mask (payload persisted to the
    scratchpad) → summarize → PostCompact with stage + token counts."""
    hooks = _FakeHooks()
    order: list[str] = []

    async def summarizer(messages, instructions=None):
        order.append("summarizer")
        return "SUMMARY"

    pad = tmp_path / "pad"
    pad.mkdir()
    deps = _make_deps(tmp_path, mode=Mode.ask, hooks=hooks)
    deps.get_scratchpad = lambda: pad   # if Deps is frozen, pass via _make_deps kwarg
    ctrl = SessionController(
        None, None, deps, max_context_tokens=10, keep_last_messages=1,
        summarizer=summarizer, mask_observations=True,
    )
    ctrl.history = _bulky_tool_history()
    assert await ctrl.maybe_compact() is True

    # Order: verdict fired before the summarizer, PostCompact after it.
    assert hooks.events[0] == f"verdict:{hook_events.PRE_COMPACT}"
    assert hooks.events[-1] == f"dispatch:{hook_events.POST_COMPACT}"
    assert order == ["summarizer"]

    # Elided payloads landed in the scratchpad and placeholders point at them.
    files = list((pad / "elided").glob("*.txt"))
    assert files
    pointers = [
        p.content
        for m in ctrl.history
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolReturnPart) and str(p.content).startswith(ELIDED_POINTER_PREFIX)
    ]
    assert pointers and str(pad) in pointers[0]

    # PostCompact payload carries the observability fields.
    post = hooks.payloads[hook_events.POST_COMPACT]
    assert post["trigger"] == "auto"
    assert post["pre_compact_tokens"] > post["post_compact_tokens"]
    assert post["stage"] in {"micro", "summary", "micro+summary"}
```

Notes for the implementer: `_make_deps` lives in `tests/conftest.py` and forwards `hooks=`; check whether `Deps` is a frozen dataclass before assigning `get_scratchpad` post-construction (if frozen, add a `get_scratchpad=` passthrough to `_make_deps` or build `Deps(...)` directly as `test_pre_compact_fires_when_compaction_runs` does).

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest --no-cov tests/test_session.py -k "stage1 or breaker or verdict or post_compact_hook or scratchpad" -v`
Expected: FAIL — no `breaker`/`on_notice` attribute, no `trigger` kwarg

- [ ] **Step 3: Implement the controller changes**

In `ctrl.py` `__init__`, after `self._autoname_task = None` add:

```python
        # Rapid-refill breaker for auto-compaction plus its one-shot notice
        # flag. In-memory only: a resumed session re-measures, so persisting
        # breaker state would carry stale thrash verdicts across restarts.
        self.breaker = CompactionBreaker()
        self._breaker_noticed = False
        self.on_notice: Callable[[str], None] | None = None
```

In `_load_active_store` (covers `new_session` and `switch_session`), add alongside the existing state resets:

```python
        self.breaker.reset()
        self._breaker_noticed = False
```

Then verify the `/clear` path also resets: `grep -n "reset_conversation\|def clear" src/marim_harness/session/ctrl.py src/marim_harness/interfaces/tui/app.py`. If `/clear` (app.py `reset_conversation`, ~line 704) reaches the controller through a method that does NOT call `_load_active_store`, add the same two reset lines to that method too — a cleared conversation must always start with a closed breaker.

Add two small private helpers above `maybe_compact` (keeps C901 under the cap):

```python
    def _elided_persist(self) -> Callable[[str, str], str | None] | None:
        """The persist callback for mask_stale_observations, or None when the
        scratchpad is unavailable — masking then degrades to plain placeholders."""
        get = getattr(self.deps, "get_scratchpad", None)
        if get is None:
            return None
        pad = get()
        if pad is None:
            return None

        def persist(content: str, tool_name: str) -> str | None:
            path = persist_elided(pad, content, tool_name)
            return str(path) if path is not None else None

        return persist

    async def _dispatch_pre_compact(
        self, trigger: str, instructions: str | None
    ) -> HookVerdict:
        if self.deps.hooks is None:
            return HookVerdict()
        return await self.deps.hooks.dispatch_verdict(
            hook_events.PRE_COMPACT,
            base_payload(
                hook_events.PRE_COMPACT,
                session_id=self.store.session_id if self.store is not None else "",
                cwd=str(self.deps.workspace.root),
                transcript_path=str(self.store.path) if self.store is not None else "",
                trigger=trigger,
                custom_instructions=instructions or "",
            ),
        )

    async def _dispatch_post_compact(
        self, trigger: str, pre_tokens: int, post_tokens: int, stage: str
    ) -> None:
        if self.deps.hooks is None:
            return
        await self.deps.hooks.dispatch(
            hook_events.POST_COMPACT,
            base_payload(
                hook_events.POST_COMPACT,
                session_id=self.store.session_id if self.store is not None else "",
                cwd=str(self.deps.workspace.root),
                transcript_path=str(self.store.path) if self.store is not None else "",
                trigger=trigger,
                pre_compact_tokens=pre_tokens,
                post_compact_tokens=post_tokens,
                stage=stage,
            ),
        )
```

Replace `maybe_compact` wholesale (preserving/adapting its long comments — the persist-now comment, the cache-safety comment on masking, and the PreCompact-before-work comment all still apply and should survive in their new positions):

```python
    async def maybe_compact(
        self,
        *,
        force: bool = False,
        trigger: str = "auto",
        instructions: str | None = None,
    ) -> bool:
        """Run the staged reduction pipeline: mask stale tool observations
        first, then summarize-compact only if the history is still over
        threshold. ``force`` is the post-overflow path (the estimate is known
        to have undershot); ``trigger="manual"`` is the /compact command —
        it bypasses the size gate and the breaker, and is the only trigger a
        PreCompact hook can block. Returns True if the history shrank."""
        before = len(self.history)
        manual = trigger == "manual"
        if manual:
            self.breaker.reset()
            self._breaker_noticed = False
        else:
            self.breaker.note_turn()
        if self.limits is not None:
            model_id = self.get_model_id() if self.get_model_id else None
            await self.limits.resolve(model_id)
        threshold = self.compact_threshold
        pre_tokens = _measured_or_estimated(self.history, self.last_input_tokens)
        over = pre_tokens > threshold
        if not (over or force or manual):
            return False
        if over and not (manual or force) and self.breaker.open:
            # Thrashing: compacting again would just refill. Skip auto
            # compaction and tell the user once what to do about it.
            if not self._breaker_noticed and self.on_notice is not None:
                self._breaker_noticed = True
                self.on_notice(BREAKER_NOTICE)
            return False
        verdict = await self._dispatch_pre_compact(trigger, instructions)
        if verdict.blocked:
            if manual:
                if self.on_notice is not None:
                    reason = f": {verdict.reason}" if verdict.reason else ""
                    self.on_notice(f"Compaction blocked by PreCompact hook{reason}")
                return False
            # A hook must never be able to wedge a session into the hard
            # context limit, so block verdicts are advisory on auto/force.
            logger.info("PreCompact block ignored (trigger=%s): %s", trigger, verdict.reason)
        indicator_shown = self.on_compact_start is not None
        if self.on_compact_start is not None:
            self.on_compact_start()
        stages: list[str] = []
        # STAGE 1 — microcompact: elide stale tool observations (persisting
        # payloads to the scratchpad when available). Runs before the
        # summarizer so that when old tool output IS the bloat, we get under
        # threshold without a model call. Cache-safe: this only ever runs when
        # the gate has tripped, i.e. when a history rewrite (and its cache
        # miss) was about to happen anyway. Force/manual run it regardless of
        # the routine-hygiene toggle — force is recovery of last resort, and a
        # manual /compact asks for maximum reduction.
        if self.mask_observations or force or manual:
            masked_history, n_masked = mask_stale_observations(
                self.history,
                self.mask_keep_recent,
                min_chars=self.mask_min_chars,
                persist=self._elided_persist(),
            )
            if n_masked:
                self.history = masked_history
                stages.append("micro")
        # STAGE 2 — summarize-compact, only if still over (manual/force always
        # proceed: the user or the overflow retry asked for a real compaction).
        # After a stage-1 mask the provider's measured count is stale (the
        # history just shrank under it), so the tail planner runs on the
        # estimate alone in that case.
        still_over = estimate_tokens(self.history) > threshold
        if manual or force or still_over:
            measured = None if "micro" in stages else self.last_input_tokens
            tail_start = _plan_tail_start(
                self.history, threshold, self.keep_last_messages,
                force=force or manual, measured_tokens=measured,
            )
            if tail_start is not None:
                if self.summarizer is not None:
                    new_history, did = await compact_history_with_summary(
                        self.history, threshold, self.summarizer,
                        self.keep_last_messages, force=force or manual,
                        tail_start=tail_start, instructions=instructions,
                    )
                else:
                    new_history, did = compact_history(
                        self.history, threshold, self.keep_last_messages,
                        force=force or manual, tail_start=tail_start,
                    )
                if did:
                    self.history = new_history
                    stages.append("summary")
        compacted = bool(stages)
        if compacted:
            # Persist the compacted history now: the post-turn compaction runs
            # after the turn's own persist, so without this the smaller history
            # lives only in memory until the next turn — a process death
            # between turns would lose it and leave the rollback baseline
            # diverged from disk.
            self.persist()
            self.breaker.note_compact()
            await self._dispatch_post_compact(
                trigger, pre_tokens, estimate_tokens(self.history), "+".join(stages)
            )
        # on_compact both reports the result AND clears the "compacting…"
        # notice, so it must fire whenever the notice was shown — not only
        # when history shrank.
        if self.on_compact is not None and (compacted or indicator_shown):
            self.on_compact(before, len(self.history))
        return compacted
```

Delete the old force-fallback branch (`elif force:` block, old lines ~507–526) — stage 1 now runs unconditionally on `force`, which is the same recovery. Update imports in `ctrl.py`: add `CompactionBreaker`, `BREAKER_NOTICE`, `estimate_tokens`, `_measured_or_estimated`, `HookVerdict` (from `..hooks.runner`), `persist_elided` (from `..workspace.scratchpad`). `_plan_tail_start` is already imported.

Note the `going`/`indicator` semantics change: the old code only showed the indicator when a tail cut was planned; the new pipeline shows it whenever the gate trips (masking may be all that happens). That is intended.

- [ ] **Step 4: Wire `on_notice` through `bind_ui`**

`runtime/harness.py` `bind_ui`: add parameter `on_notice: Callable[[str], None] | None = None` (next to `on_compact`, line ~502) and `self.session.on_notice = on_notice` next to line 537.

`interfaces/tui/app.py`: in the `bind_ui` call (~line 132) add `on_notice=self._on_session_notice,` and define next to `_on_compact` (~line 661):

```python
    def _on_session_notice(self, message: str) -> None:
        """Session-level advisory (breaker tripped, manual compact blocked).
        Same call-from-anywhere contract as _on_compact."""
        self._append_log(NoticeMessage(message))
```

(`NoticeMessage` is already imported in `app.py`.)

- [ ] **Step 5: Run and repair the session suite**

Run: `uv run pytest --no-cov tests/test_session.py tests/test_compaction.py -v`

Expected new tests PASS. Pre-existing tests that may need updating for the reorder — fix expectations, not behavior:
- the PRE_COMPACT ordering test (~line 609: `order == ["hook:PreCompact", "summarizer"]`) still holds — hook fires before stage 2.
- the forced-compaction test (~line 670) now succeeds via stage 1 instead of the deleted fallback; assertions on placeholder text must accept pointer placeholders (`_is_masked`) if a scratchpad is wired in that fixture (it isn't by default — plain `MASKED_OBSERVATION` stays).
- any test asserting masking happens *after* compaction (mask applied to the tail only) needs its expectation inverted: masking now precedes the cut, so masked returns can also appear pre-cut. `uv run pytest --no-cov tests/test_session.py -v 2>&1 | grep FAILED` and fix each against the new pipeline semantics.

Also run: `uv run pytest --no-cov tests/test_run_failure_recovery.py tests/test_provider_errors.py -v` (they exercise `maybe_compact(force=True)` through the controller).

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/session/ctrl.py src/marim_harness/runtime/harness.py \
        src/marim_harness/interfaces/tui/app.py tests/test_session.py
git commit -m "feat(session): staged mask-then-summarize compaction pipeline

Stage 1 elides stale tool observations (persisted to the scratchpad when
available) and can satisfy the whole reduction without a summarizer call;
stage 2 summarize-compacts only if still over threshold. Adds the rapid-
refill breaker, manual trigger semantics, PreCompact verdicts (blocking
honored on manual only) and the PostCompact event."
```

---

### Task 7: `/compact [instructions]` command

**Files:**
- Modify: `src/marim_harness/interfaces/tui/commands.py`
- Test: `tests/test_commands.py` (append)

**Interfaces:**
- Consumes: `maybe_compact(trigger="manual", instructions=...)` (Task 6); `app.turn_busy`, `app.post_system`, `app.run_worker` (existing).
- Produces: `/compact` registered in `COMMANDS`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_commands.py` (extend `_FakeApp` — see its definition at line ~18 — with `turn_busy = False`, a `run_worker` that executes the coroutine immediately via `asyncio.get_event_loop().create_task`… simplest is to make the fake's `run_worker(coro, **kw)` just schedule `await coro`; follow how existing async command tests drive handlers):

```python
async def test_compact_refuses_while_turn_busy():
    app = _FakeApp()
    app.turn_busy = True
    await dispatch(app, "/compact")
    assert any("turn is running" in m for m in app.system_messages)


async def test_compact_passes_manual_trigger_and_instructions():
    app = _FakeApp()
    calls = {}

    async def fake_maybe_compact(*, force=False, trigger="auto", instructions=None):
        calls.update(trigger=trigger, instructions=instructions)
        return True

    app.harness.session.maybe_compact = fake_maybe_compact
    await dispatch(app, "/compact focus on the auth bug")
    await app.drain_workers()          # however _FakeApp settles scheduled work
    assert calls == {"trigger": "manual", "instructions": "focus on the auth bug"}


async def test_compact_reports_nothing_to_do():
    app = _FakeApp()

    async def fake_maybe_compact(**kw):
        return False

    app.harness.session.maybe_compact = fake_maybe_compact
    await dispatch(app, "/compact")
    await app.drain_workers()
    assert any("Nothing to compact" in m for m in app.system_messages)
```

Adapt the fake-app plumbing to what `_FakeApp` actually provides (it already fakes `post_system` for other tests; add `harness.session` as a `SimpleNamespace` and a worker shim if absent).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov tests/test_commands.py -k compact -v`
Expected: FAIL — unknown command `/compact`

- [ ] **Step 3: Implement**

In `commands.py` add the handler (place near `_cmd_clear`):

```python
async def _cmd_compact(app: HarnessApp, arg: str) -> None:
    """Manually compact the session: mask stale tool output, then summarize.
    Runs in its own worker group — the summarizer can take a while, and the
    default worker group would let a starting turn cancel it (WorkerManager
    sweeps a group when an exclusive worker joins; see _handle_bang)."""
    if app.turn_busy:
        await app.post_system("Can't compact while a turn is running. Press Esc first.")
        return

    async def run() -> None:
        did = await app.harness.session.maybe_compact(
            trigger="manual", instructions=arg or None
        )
        if not did:
            await app.post_system("Nothing to compact.")

    app.run_worker(run(), group="compact", exclusive=True, exit_on_error=False)
```

Register it in `COMMANDS` after the `clear` entry:

```python
    Command(
        "compact",
        "free context now: /compact [summary instructions]",
        _cmd_compact,
    ),
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_commands.py -v`
Expected: all PASS (including `test_every_command_has_summary_and_handler`, which picks up the new entry automatically)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/commands.py tests/test_commands.py
git commit -m "feat(tui): /compact command — manual compaction with summary instructions"
```

---

### Task 8: Docs + full gate

**Files:**
- Modify: `CLAUDE.md` (hooks bullet in "Supporting subsystems")

**Interfaces:** none — documentation and verification only.

- [ ] **Step 1: Update CLAUDE.md**

In the `hooks/` bullet, change

> Observe-only except SessionStart/UserPromptSubmit, which inject context.

to

> Observe-only except SessionStart/UserPromptSubmit (inject context) and PreCompact (may block a *manual* /compact via exit 2 or `{"decision":"block"}`; block verdicts on auto compaction are logged and ignored).

- [ ] **Step 2: Full CI gate, in CI's order**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: ruff clean; pyright clean; full suite green with coverage ≥ the pre-change baseline. Fix anything that fails before committing (C901 on `maybe_compact` is the likely tripwire — if it fires, extract the stage-2 block into a `_stage_summarize` helper rather than suppressing).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: PreCompact manual-blocking exception in the hooks contract"
```
