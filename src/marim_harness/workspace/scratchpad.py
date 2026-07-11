"""Session-scoped scratchpad directory under the system temp dir.

The scratchpad lives OUTSIDE the workspace on purpose: it exists so the
agent's intermediate artifacts (temp scripts, staged outputs, analysis
files) don't pollute the project tree or its git status. Pure path
derivation (scratchpad_root) is separated from the impure ensure_scratchpad
(mkdir + /tmp-squatting check) per the repo's pure-helper convention.
See docs/superpowers/specs/2026-07-11-scratchpad-design.md.
"""

import hashlib
import logging
import os
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Bases that already failed the squatting check (or mkdir) and were warned
# about. Module-level so the per-model-request instructions closure and the
# per-tool-call getter don't spam the log with the same warning.
_warned: set[Path] = set()


def scratchpad_base() -> Path:
    """The per-user base every scratchpad lives under (``/tmp/marim-<uid>``).

    Keyed by uid so two users on a shared machine can't collide — and, with
    the ownership check in ensure_scratchpad, can't squat each other's base.
    """
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"marim-{uid}"


def scratchpad_root(
    workspace_root: Path, session_id: str, base: Path | None = None
) -> Path:
    """The scratchpad dir for one session. Pure — no filesystem access.

    ``<base>/<workspace-slug>/<session-id>/scratchpad``. The workspace slug
    (``{name}-{sha256(root)[:12]}``) deliberately matches session storage's
    naming (session/store.py::_workspace_dir) so scratchpads key the same
    way sessions do; a test pins the parity. The ``scratchpad`` leaf leaves
    room for future per-session sidecars in the same directory.
    """
    digest = hashlib.sha256(str(workspace_root).encode()).hexdigest()[:12]
    b = base if base is not None else scratchpad_base()
    return b / f"{workspace_root.name}-{digest}" / session_id / "scratchpad"


def ensure_scratchpad(
    workspace_root: Path, session_id: str, base: Path | None = None
) -> Path | None:
    """Create (if needed) and return the session's scratchpad dir, or None
    when it can't be provided safely — callers treat None as "feature off".

    The base dir is created 0o700 and then verified: it must be a real
    directory (not a symlink) owned by the current uid. A pre-existing
    symlink or foreign-owned dir is classic /tmp squatting — someone
    pre-creating the path to redirect or read our writes — so refuse and
    disable rather than proceed. Each failing base warns once (see _warned).
    """
    b = base if base is not None else scratchpad_base()
    try:
        b.mkdir(mode=0o700, exist_ok=True)
        st = b.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise OSError(f"{b} exists but is not a real directory")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise OSError(f"{b} is owned by uid {st.st_uid}, not {os.getuid()}")
        root = scratchpad_root(workspace_root, session_id, base=b)
        root.mkdir(parents=True, exist_ok=True)
        return root
    except OSError as exc:
        if b not in _warned:
            _warned.add(b)
            logger.warning("scratchpad disabled: %s", exc)
        return None
