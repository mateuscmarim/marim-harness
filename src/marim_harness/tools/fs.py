import bisect
import fnmatch
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import ModelRetry

from ..atomic_io import atomic_write_text
from ..workspace.fs import ReadLedger, WorkspaceError, resolve_in_workspace
from .offload import MAX_OUTPUT_CHARS, offload_if_large

# Capture the process umask once at import time so we never need to manipulate
# os.umask() at runtime — that API is process-global and not thread-safe.
_DEFAULT_UMASK: int = os.umask(0o022)
os.umask(_DEFAULT_UMASK)

# When no explicit ``limit`` is given, a read is capped at this many lines so a
# blind read of a huge file can't flood the context. Pass ``offset``/``limit``
# to page through the rest. An explicit ``limit`` overrides this cap.
_DEFAULT_READ_LIMIT = 500

# The line cap bounds *how many* lines come back, but not their width — a minified
# bundle, single-line JSON, or wide CSV could still flood context within the line
# limit. Two byte-level guards bound the result regardless of file shape:
#   * each line is clipped to _MAX_LINE_CHARS (kills the few-enormous-lines case);
#   * the whole read stops once _MAX_READ_CHARS is reached (the many-wide-lines
#     case), ending the window early with a footer so the model pages on.
# Unlike the line cap, these apply even when an explicit ``limit`` is given: the
# limit says how many lines you want, the byte budget says how much can be
# returned. At least one line always comes back so a read never returns empty.
_MAX_LINE_CHARS = 2_000
_MAX_READ_CHARS = 100_000

# Directories that are almost always noise: tree lists them without expanding;
# grep skips them entirely (the dominant cost of searching a large repo is
# descending into .git/node_modules/.venv rather than the real source).
_NOISE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".egg-info",
    ".worktrees",
}


def _safe(root: Path, path: str) -> Path:
    try:
        return resolve_in_workspace(root, path)
    except WorkspaceError as exc:
        raise ModelRetry(str(exc)) from exc


def _require_read_before_write(ledger: ReadLedger | None, p: Path, path: str) -> None:
    """Raise a model-facing ModelRetry if ``p`` may not be modified yet: it was
    never read this session, or it changed on disk since it was read. A ``None``
    ledger disables the guard (direct callers / tests that opt out). The reason
    code from the framework-free ``ReadLedger`` is turned into the guidance the
    model needs — the wording references ``read_file`` because the tool layer,
    not ``workspace.fs``, owns tool-name-specific messaging."""
    if ledger is None:
        return
    reason = ledger.staleness(p)
    if reason == "unread":
        raise ModelRetry(
            f"{path}: read the file before editing it (call read_file first). This "
            f"guards against modifying a file you haven't seen this session."
        )
    if reason == "changed":
        raise ModelRetry(
            f"{path} changed on disk since you read it — read it again before "
            f"editing, as its current content may differ from what you saw."
        )


def _safe_read(root: Path, path: str, extra_read_roots: tuple[Path, ...]) -> Path:
    """Resolve ``path`` for reading, permitting it if it stays inside ``root`` or
    inside any of ``extra_read_roots``. The extra roots are read-only escape hatches
    (e.g. skill directories that live outside the workspace) — they widen reads only,
    never writes, and only to files genuinely inside one of them."""
    try:
        return resolve_in_workspace(root, path)
    except WorkspaceError as exc:
        for extra in extra_read_roots:
            try:
                return resolve_in_workspace(extra, path)
            except WorkspaceError:
                continue
        raise ModelRetry(str(exc)) from exc


def read_file(
    root: Path,
    path: str,
    offset: int = 1,
    limit: int | None = None,
    extra_read_roots: tuple[Path, ...] = (),
    ledger: ReadLedger | None = None,
) -> str:
    """Read a text file relative to the workspace root, returning numbered lines.

    ``offset`` is the 1-based line to start at; ``limit`` caps how many lines are
    returned. With no ``limit``, the read is capped at ``_DEFAULT_READ_LIMIT``
    lines so a huge file can't flood the context. Wide content is bounded too:
    over-long lines are clipped to ``_MAX_LINE_CHARS`` and the read stops once it
    has emitted ``_MAX_READ_CHARS`` worth of text. When the returned window isn't
    the whole file (or a line was clipped), a ``[…]`` footer says so, so the
    reader knows to page on with ``offset``/``limit``.

    ``extra_read_roots`` are additional directories a path may resolve into besides
    the workspace (read-only) — used to let reads reach skill directories that live
    outside the workspace."""
    if offset < 1:
        raise ModelRetry("offset must be >= 1 (1-based line number).")
    if limit is not None and limit < 1:
        raise ModelRetry("limit must be >= 1.")
    p = _safe_read(root, path, extra_read_roots)
    if not p.is_file():
        raise ModelRetry(f"not a file: {path}")
    # Mark the file seen for the read-before-edit guard. Fingerprinted now,
    # regardless of which window is returned: the agent has observed this file,
    # which is what edit_file/write_file require — they don't demand the exact
    # region was read.
    if ledger is not None:
        ledger.record(p)
    start = offset - 1
    span = limit if limit is not None else _DEFAULT_READ_LIMIT
    end = start + span

    # Stream the file rather than ``read_text().splitlines()``: the old path
    # materialized the *entire* file (plus a Python list holding every line),
    # so paging lines 9000-9010 of a 500 MB file dragged all 500 MB into memory.
    # Here only the requested window [start, end) is retained; the rest of the
    # file is iterated but discarded, kept solely to count ``total`` for the
    # "of {total}" footer (no way to know a variable-width file's line count
    # without reading it, and the output contract requires the total). Memory is
    # bounded by the window, not the file size. Universal-newline text mode (the
    # default) folds ``\r\n``/``\r`` into ``\n`` the same way ``splitlines`` does
    # for the common separators, and we strip the one trailing ``\n`` per line to
    # match ``splitlines``' terminator-free output; only the exotic separators
    # (``\v``, ``\f``, U+2028, …) that ``splitlines`` also breaks on are treated
    # as in-line text here, which never arises for normal-size text files.
    window: list[str] = []
    total = 0
    with p.open("r", errors="replace") as fh:
        for i, raw in enumerate(fh):
            if start <= i < end:
                window.append(raw[:-1] if raw.endswith("\n") else raw)
            total = i + 1
    if total == 0:
        return ""
    if offset > total:
        raise ModelRetry(f"offset {offset} is past end of file ({total} lines).")

    rendered: list[str] = []
    used = 0
    clipped = False
    for idx, line in enumerate(window):
        lineno = start + idx + 1
        if len(line) > _MAX_LINE_CHARS:
            extra = len(line) - _MAX_LINE_CHARS
            line = f"{line[:_MAX_LINE_CHARS]}… (+{extra} more chars on this line)"
            clipped = True
        row = f"{lineno}\t{line}"
        # Stop before the char budget is exceeded, but always emit at least one
        # row so a read never comes back empty (a single wide line still returns,
        # clipped to _MAX_LINE_CHARS).
        if rendered and used + len(row) + 1 > _MAX_READ_CHARS:
            break
        rendered.append(row)
        used += len(row) + 1

    last = start + len(rendered)  # 1-based number of the last line included
    body = "\n".join(rendered)
    windowed = not (start == 0 and last == total)
    notes: list[str] = []
    if windowed:
        notes.append(f"showing lines {offset}-{last} of {total}")
    if clipped:
        notes.append(f"long lines clipped to {_MAX_LINE_CHARS} chars")
    if not notes:
        return body  # whole file, nothing clipped — unchanged output
    return f"{body}\n\n[{'; '.join(notes)}]"


def _atomic_write_preserving_mode(p: Path, content: str) -> None:
    """Write ``content`` to ``p`` atomically, keeping the file's permission bits.

    ``atomic_write_text`` writes a ``mkstemp`` temp file (mode 0600) and
    ``os.replace``s it over the target — durable and crash-safe, but it would
    otherwise *clobber* an existing file's mode (e.g. strip ``+x`` off a script)
    and leave a newly created file owner-only. So restore the original mode on an
    overwrite, and fall back to the umask default for a new file. The brief window
    where the mode isn't yet restored is acceptable: the *content* swap is atomic,
    which is the property that matters."""
    try:
        original = stat.S_IMODE(p.stat().st_mode)
    except FileNotFoundError:
        original = None
    atomic_write_text(p, content)
    if original is not None:
        os.chmod(p, original)
    else:
        os.chmod(p, 0o666 & ~_DEFAULT_UMASK)


def write_file(
    root: Path, path: str, content: str, ledger: ReadLedger | None = None
) -> str:
    """Create or overwrite a file relative to the workspace root."""
    p = _safe(root, path)
    # Read-before-edit applies only to *overwriting* an existing file (clobbering
    # content the agent may not have seen). Creating a brand-new file needs no
    # prior read — there's nothing to clobber.
    if p.exists():
        _require_read_before_write(ledger, p, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic, like the session/checkpoint persistence layer: a crash mid-write
    # leaves the old file intact rather than a truncated one, and a parallel
    # sub-agent writing the same path can't interleave a half-written result.
    _atomic_write_preserving_mode(p, content)
    # Refresh the fingerprint so a follow-up write/edit this turn isn't flagged
    # as stale against the pre-write content.
    if ledger is not None:
        ledger.record(p)
    return f"wrote {path} ({len(content.encode('utf-8'))} bytes, {len(content)} chars)"


class Edit(BaseModel):
    """One find/replace within a file. ``replace_all`` swaps every occurrence;
    otherwise ``old_string`` must match exactly once."""

    old_string: str
    new_string: str
    replace_all: bool = False


def _apply_edit(text: str, edit: Edit, path: str, index: int) -> str:
    """Apply one edit to ``text``, raising ModelRetry (naming the edit) on a bad
    match. ``index`` is 1-based for human-readable messages."""
    # An empty old_string is a trap, not a valid edit: ``"".count`` in ``text``
    # is len+1 (every gap between chars matches), so the ambiguity guard below
    # would tell the model to "set replace_all" — and a model that obeys gets
    # ``text.replace("", new)``, which splices new_string between *every*
    # character of the file: silent, total corruption via the tool's own
    # guidance. Refuse up front; an edit must name the exact text it replaces.
    if edit.old_string == "":
        raise ModelRetry(
            f"edit {index}: old_string is empty. Provide the exact text to "
            f"replace; to insert, anchor new_string to a surrounding non-empty "
            f"snippet (old_string must appear in the file)."
        )
    count = text.count(edit.old_string)
    if count == 0:
        raise ModelRetry(
            f"edit {index}: old_string not found in {path}. Read the file and copy "
            f"an exact snippet (note earlier edits in this call may have changed it)."
        )
    if count > 1 and not edit.replace_all:
        raise ModelRetry(
            f"edit {index}: old_string found {count} times in {path}. Add surrounding "
            f"context to make it unique, or set replace_all."
        )
    return text.replace(edit.old_string, edit.new_string)


def edit_file(
    root: Path, path: str, edits: list[Edit], ledger: ReadLedger | None = None
) -> str:
    """Apply a list of edits to one file, in order and all-or-nothing. Each edit
    sees the result of the previous one; the file is written only if all succeed."""
    if not edits:
        raise ModelRetry("no edits given: pass at least one {old_string, new_string}.")
    p = _safe(root, path)
    if not p.is_file():
        raise ModelRetry(f"not a file: {path}")
    # Editing always modifies existing content, so the read-before-edit guard
    # always applies (unlike write_file, which exempts brand-new files).
    _require_read_before_write(ledger, p, path)
    # Strict decode: unlike read_file (display-only, errors="replace"), edit_file
    # reads-modifies-writes, so a lossy decode would round-trip the undecodable
    # bytes back as U+FFFD and corrupt regions the edit never touched. Refuse
    # instead, with clear feedback.
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ModelRetry(f"can't edit {path}: not a UTF-8 text file.") from None
    for i, edit in enumerate(edits, 1):
        text = _apply_edit(text, edit, path, i)
    _atomic_write_preserving_mode(p, text)  # all-or-nothing on disk too — see write_file
    # Refresh the fingerprint so a follow-up edit this turn isn't seen as stale.
    if ledger is not None:
        ledger.record(p)
    n = len(edits)
    return f"edited {path} ({n} edit{'s' if n != 1 else ''})"


def tree(root: Path, path: str = ".", depth: int = 2) -> str:
    """Render an indented directory tree rooted at ``path``, descending up to
    ``depth`` levels. Dirs sort first (with a trailing slash); known-noise dirs
    are listed but not expanded. Large trees are offloaded to a file."""
    base = _safe(root, path)
    if not base.is_dir():
        raise ModelRetry(f"not a directory: {path}")
    lines: list[str] = []
    capped = _walk_tree(base, depth, 0, lines)
    if not lines:
        return "(empty)"
    return offload_if_large(
        "\n".join(lines), kind="tree", key=f"{path}\0{depth}",
        workspace_root=root, capped=capped,
    )


def _walk_tree(directory: Path, depth: int, level: int, lines: list[str]) -> bool:
    """Append the entries of ``directory`` to ``lines``, recursing while depth
    allows. Returns True once the MAX_OUTPUT_CHARS ceiling is reached so callers
    stop early."""
    size = 0

    def _recurse(directory: Path, level: int) -> bool:
        nonlocal size
        try:
            entries = list(directory.iterdir())
        except OSError:
            return False
        entries.sort(key=lambda p: (p.is_file(), p.name.lower()))
        indent = "  " * level
        for entry in entries:
            if size >= MAX_OUTPUT_CHARS:
                return True
            if entry.is_dir():
                line = f"{indent}{entry.name}/"
                lines.append(line)
                size += len(line) + 1
                # Never descend a symlinked directory: it can point OUTSIDE the
                # workspace sandbox, and recursing would enumerate paths there
                # (``entry.is_dir()`` follows the link, so a symlink-to-dir reaches
                # here). List the symlink name, but stop — matching os.walk, which
                # every sibling read tool relies on for the same containment.
                if (
                    entry.name not in _NOISE_DIRS
                    and not entry.is_symlink()
                    and level + 1 < depth
                    and _recurse(entry, level + 1)
                ):
                    return True
            else:
                line = f"{indent}{entry.name}"
                lines.append(line)
                size += len(line) + 1
        return False

    return _recurse(directory, level)


def glob_files(root: Path, pattern: str) -> str:
    """List files under the workspace matching a glob pattern. Large match lists
    are offloaded to a file (handle + preview) instead of flooding the response."""
    try:
        candidates = list(root.glob(pattern))
    except (NotImplementedError, ValueError) as exc:
        raise ModelRetry(
            "invalid glob pattern: use a path relative to the workspace, "
            "no leading '/' or '..'"
        ) from exc
    matches = []
    size = 0
    capped = False
    for p in candidates:
        # ``Path.glob`` can't be told to prune, so it walks node_modules/.git/.venv
        # and hands back their files like any other match — unlike grep/tree, which
        # skip _NOISE_DIRS. Drop noise matches here, *before* the per-candidate
        # ``is_file`` stat and the ``resolve_in_workspace`` resolve (the dominant
        # per-file cost): the parts check is pure-path, no syscall. ``.worktrees``
        # (sibling worktree checkouts) is itself in _NOISE_DIRS, so this also
        # subsumes the old worktree skip. Only the *directory* components are
        # checked (``parts[:-1]``) — a regular file literally named ``build`` or
        # ``dist`` is a legitimate match and must not be pruned, matching how
        # grep/tree prune on directory descent rather than on the full path.
        rel_path = p.relative_to(root)
        if any(part in _NOISE_DIRS for part in rel_path.parts[:-1]):
            continue
        if not p.is_file():
            continue
        rel = str(rel_path)
        try:
            resolve_in_workspace(root, rel)
        except WorkspaceError:
            continue  # skip matches that escape the workspace root
        matches.append(rel)
        size += len(rel) + 1
        if size >= MAX_OUTPUT_CHARS:
            capped = True
            break
    if not matches:
        return "(no matches)"
    matches.sort()
    return offload_if_large(
        "\n".join(matches), kind="glob", key=pattern,
        workspace_root=root, capped=capped,
    )


# A NUL byte in the first chunk of a file marks it binary (grep skips it). This
# bounds how much we sniff before deciding — a real text file rarely hides its
# first NUL past 8 KB, and a binary blob trips the check without our reading the
# whole thing (see ``_read_text_for_grep``).
_BINARY_SNIFF_BYTES = 8192


def _read_text_for_grep(path: Path) -> str | None:
    """Read ``path`` for grep in a single open, returning its decoded text or
    ``None`` if it should be skipped (unreadable, or binary by the NUL-byte
    sniff). This folds what used to be a separate ``_is_binary`` open+read and a
    full ``read_text`` into one pass: the head is read first so a binary blob is
    rejected *without* slurping the rest of the (often huge) file, and only a
    non-binary file is read to completion. ``read_text(errors="replace")`` decoded
    UTF-8 under universal newlines, so we replicate both: decode replacing bad
    bytes, then fold ``\\r\\n``/``\\r`` into ``\\n`` (guarded on the common
    LF-only case) so carriage returns don't leak into match output or perturb the
    multiline regex."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_BINARY_SNIFF_BYTES)
            if b"\x00" in head:
                return None
            data = head + fh.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _walk_files(base: Path) -> Iterator[Path]:
    """Yield files under ``base``, pruning noise dirs (.git, node_modules, …) so a
    search never descends into them. ``os.walk`` does not follow symlinked dirs by
    default, so the walk cannot wander outside the tree.  Unreadable directories
    (PermissionError) are skipped — the walk continues with the rest of the tree."""
    if base.is_file():
        yield base
        return
    try:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS]
            for name in filenames:
                yield Path(dirpath) / name
    except PermissionError:
        pass


# Common ripgrep ``--type`` names mapped to the file extensions they cover, so the
# model can scope a search by language (``type="py"``) the way it does in Claude
# Code. An unrecognized name falls back to a literal extension match (see
# ``_type_extensions``), so a niche type still does something sensible.
_TYPE_EXTENSIONS: dict[str, set[str]] = {
    "py": {".py", ".pyi"}, "python": {".py", ".pyi"},
    "js": {".js", ".jsx", ".mjs", ".cjs"},
    "ts": {".ts", ".tsx", ".mts", ".cts"},
    "rust": {".rs"}, "rs": {".rs"},
    "go": {".go"},
    "java": {".java"},
    "kotlin": {".kt", ".kts"},
    "c": {".c", ".h"},
    "cpp": {".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"},
    "cs": {".cs"},
    "rb": {".rb"}, "ruby": {".rb"},
    "php": {".php"},
    "swift": {".swift"},
    "sh": {".sh", ".bash", ".zsh"}, "bash": {".sh", ".bash"},
    "json": {".json"},
    "yaml": {".yaml", ".yml"}, "yml": {".yaml", ".yml"},
    "toml": {".toml"},
    "md": {".md", ".markdown"}, "markdown": {".md", ".markdown"},
    "html": {".html", ".htm"},
    "css": {".css", ".scss", ".sass", ".less"},
    "xml": {".xml"},
    "sql": {".sql"},
    "txt": {".txt"},
}

_GREP_MODES = ("content", "files_with_matches", "count")


def _type_extensions(file_type: str) -> set[str]:
    """Extensions a ripgrep ``--type`` name covers; unknown names fall back to a
    single literal ``.<name>`` extension."""
    return _TYPE_EXTENSIONS.get(file_type.lower(), {f".{file_type.lstrip('.')}"})


def _brace_expand(glob: str) -> list[str]:
    """Expand a single-level brace alternation (``*.{ts,tsx}`` → ``*.ts``,
    ``*.tsx``) into concrete fnmatch patterns. Recurses so multiple groups expand;
    a glob with no braces returns unchanged."""
    m = re.search(r"\{([^{}]*)\}", glob)
    if not m:
        return [glob]
    pre, post = glob[: m.start()], glob[m.end() :]
    out: list[str] = []
    for opt in m.group(1).split(","):
        out.extend(_brace_expand(pre + opt + post))
    return out


def _match_glob(rel: str, name: str, patterns: list[str]) -> bool:
    """True if the file matches any glob. A pattern with no ``/`` matches the
    basename (``*.py`` hits ``src/a.py``); otherwise it matches the whole relative
    path. A leading ``**/`` is also tried against the basename so ``**/*.py``
    behaves like ripgrep regardless of depth."""
    for p in patterns:
        target = rel if "/" in p.replace("**/", "") else name
        if fnmatch.fnmatchcase(target, p):
            return True
        if p.startswith("**/") and fnmatch.fnmatchcase(name, p[3:]):
            return True
    return False


def _context_ranges(
    matches: list[int], before: int, after: int, n_lines: int
) -> list[tuple[int, int]]:
    """Merge each match's [i-before, i+after] window into ordered, non-overlapping
    inclusive line ranges (adjacent windows coalesce, like ripgrep)."""
    ranges: list[tuple[int, int]] = []
    for i in matches:
        lo, hi = max(0, i - before), min(n_lines - 1, i + after)
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], hi))
        else:
            ranges.append((lo, hi))
    return ranges


class _OutputCollector:
    """Accumulates grep output lines while tracking size and limit budgets."""

    def __init__(self, head_limit: int | None) -> None:
        self.out: list[str] = []
        self._size = 0
        self._head_limit = head_limit
        self.capped = False
        self.limited = False
        self.stop = False

    def emit(self, line: str) -> None:
        self.out.append(line)
        self._size += len(line) + 1
        if self._size >= MAX_OUTPUT_CHARS:
            self.capped = self.stop = True
        if self._head_limit is not None and len(self.out) >= self._head_limit:
            self.limited = self.stop = True


def grep(
    root: Path,
    pattern: str,
    path: str | None = None,
    *,
    glob: str | None = None,
    file_type: str | None = None,
    output_mode: str = "content",
    head_limit: int | None = None,
    case_insensitive: bool = False,
    before_context: int = 0,
    after_context: int = 0,
    multiline: bool = False,
) -> str:
    """Search file contents for a regex. ``output_mode`` picks the shape:
    ``content`` → ``relpath:line:text`` hits (context lines, when requested, use a
    ``-`` separator and ``--`` between groups, like ripgrep); ``files_with_matches``
    → one ``relpath`` per matching file; ``count`` → ``relpath:count`` per file.
    Skips noise dirs (.git, node_modules, .venv, …) and binary files; ``glob`` /
    ``file_type`` scope which files are searched; ``head_limit`` caps the number of
    output rows; large results are offloaded to a file; collection stops at
    MAX_OUTPUT_CHARS."""
    if output_mode not in _GREP_MODES:
        raise ModelRetry(
            f"invalid output_mode {output_mode!r}; use one of {', '.join(_GREP_MODES)}."
        )
    re_flags = 0
    if case_insensitive:
        re_flags |= re.IGNORECASE
    if multiline:
        re_flags |= re.DOTALL | re.MULTILINE
    try:
        rx = re.compile(pattern, re_flags)
    except re.error as exc:
        raise ModelRetry(f"invalid regex {pattern!r}: {exc}") from exc
    base = _safe(root, path) if path else root
    globs = _brace_expand(glob) if glob else None
    exts = _type_extensions(file_type) if file_type else None
    before, after = max(0, before_context), max(0, after_context)

    col = _OutputCollector(head_limit)

    for f in _walk_files(base):
        if col.stop:
            break
        # os.walk won't descend symlinked dirs; a symlinked *file* could still
        # point outside the workspace, so gate just that rare case.
        if f.is_symlink():
            try:
                resolve_in_workspace(root, str(f.relative_to(root)))
            except (WorkspaceError, ValueError):
                continue
        if not f.is_file():
            continue
        rel = f.relative_to(root).as_posix()
        # Apply the cheap, syscall-free glob/type filters *before* opening the
        # file: a file excluded by name shouldn't pay for a binary sniff + read.
        if globs is not None and not _match_glob(rel, f.name, globs):
            continue
        if exts is not None and f.suffix not in exts:
            continue
        text = _read_text_for_grep(f)
        if text is None:
            continue  # unreadable or binary (NUL-byte sniff)

        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()  # a trailing newline isn't an extra empty line
        # ``match_idx`` is the sorted set of matching line numbers; ``n_matches`` is
        # the match count for ``count`` mode. In multiline mode a single match can
        # span lines, so every line it covers is a match line — which lets the same
        # content-emission path below apply context (-A/-B/-C) uniformly instead of
        # silently dropping it.
        if multiline:
            hits = list(rx.finditer(text))
            n_matches = len(hits)
            # Map each match's char offsets to 0-based line numbers. The old code
            # did ``text.count("\n", 0, pos)`` per match — O(n) per call, O(n·k)
            # for k matches. Instead precompute the newline offsets once and bisect:
            # the line index of ``pos`` is the count of newlines strictly before it,
            # which is exactly ``bisect_left(nl_offsets, pos)`` (O(log n) per match).
            # ``str.find`` builds the offset list with C-level scans rather than a
            # per-character Python loop.
            nl_offsets: list[int] = []
            j = text.find("\n")
            while j != -1:
                nl_offsets.append(j)
                j = text.find("\n", j + 1)
            covered: set[int] = set()
            for m in hits:
                lo = bisect.bisect_left(nl_offsets, m.start())
                hi = min(bisect.bisect_left(nl_offsets, m.end()), len(lines) - 1)
                covered.update(range(lo, hi + 1))
            match_idx = sorted(covered)
        else:
            match_idx = [i for i, ln in enumerate(lines) if rx.search(ln)]
            n_matches = len(match_idx)
        if not match_idx:
            continue
        if output_mode == "files_with_matches":
            col.emit(rel)
        elif output_mode == "count":
            col.emit(f"{rel}:{n_matches}")
        else:
            match_set = set(match_idx)
            for gi, (lo, hi) in enumerate(
                _context_ranges(match_idx, before, after, len(lines))
            ):
                if (before or after) and gi:
                    col.emit("--")
                    if col.stop:
                        break
                for i in range(lo, hi + 1):
                    sep = ":" if i in match_set else "-"
                    col.emit(f"{rel}{sep}{i + 1}{sep}{lines[i]}")
                    if col.stop:
                        break
                if col.stop:
                    break

    if not col.out:
        return "(no matches)"
    body = "\n".join(col.out)
    if col.limited:
        body += f"\n(stopped at head_limit={head_limit})"
    key = f"{pattern}\0{path or ''}\0{output_mode}\0{glob or ''}\0{file_type or ''}"
    return offload_if_large(body, kind="grep", key=key, workspace_root=root, capped=col.capped)
