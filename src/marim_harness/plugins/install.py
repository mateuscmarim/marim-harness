"""Install, update, and manage the lifecycle of plugins.

Installing copies (or, with ``link``, symlinks) a plugin's directory into the
target scope's ``plugins/`` cache, validates its manifest strictly, and records
it in the scope registry. A plugin with no executable parts (hooks/MCP) is
auto-trusted; one with executable parts is trusted only when ``trust`` is set.
Git sources are shallow-cloned to a temp dir and copied in, recording the
resolved commit SHA."""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .discovery import has_executable, plugin_bundle_summary
from .manifest import ManifestError, PluginManifest, load_manifest, valid_plugin_name
from .state import (
    InstalledPlugin,
    global_plugins_dir,
    load_state,
    project_plugins_dir,
    save_state,
)

logger = logging.getLogger(__name__)


class InstallError(Exception):
    """An install/update/lifecycle operation failed."""


def scope_dir(scope: str, workspace_root) -> Path:
    if scope == "global":
        return global_plugins_dir()
    if scope == "project":
        return project_plugins_dir(workspace_root)
    raise InstallError(f"unknown scope: {scope!r} (use 'global' or 'project')")


def is_git_source(source: str) -> bool:
    s = source.strip()
    if s.startswith(("http://", "https://", "git://", "ssh://")):
        return True
    if s.startswith("git@"):
        return True
    return s.endswith(".git")


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise InstallError("git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise InstallError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return out.stdout.strip()


def _reject_option_like(value: str, kind: str) -> None:
    """Refuse a git ``source``/``ref`` that begins with ``-``.

    Both flow in from the plugin registry, and a *project* registry
    (``.marim/plugins/plugins.json``) is committed to the repo — so a hostile
    repo controls these strings and ``marim plugin update`` on a clone would use
    them verbatim. Git reads a leading-dash argument as an option, so a recorded
    url like ``--upload-pack=<cmd>`` makes ``git clone`` execute an arbitrary
    command. The ``--`` separator added in ``_clone_git`` stops a *positional*
    from being parsed as an option, but ``ref`` lands in an option slot
    (``--branch <ref>``) where ``--`` can't protect it, so a leading-dash value
    must be rejected outright before either token is handed to git."""
    if value.startswith("-"):
        raise InstallError(f"refusing {kind} that looks like a git option: {value!r}")


def _clone_git(source: str, dest: Path, ref: str | None) -> dict:
    """Clone ``source`` into ``dest`` and return a source record with the resolved SHA."""
    _reject_option_like(source, "git source")
    if ref is not None:
        _reject_option_like(ref, "git ref")
    if ref:
        # Try ``ref`` as a branch/tag first (cheap shallow clone). A commit SHA
        # is not a valid ``--branch`` argument, so on failure fall back to a full
        # clone + checkout, which resolves any ref including a SHA. ``--`` before
        # the positional url/dest keeps a crafted url from being read as an option.
        try:
            _run_git(["clone", "--depth", "1", "--branch", ref, "--", source, str(dest)])
        except InstallError:
            if dest.exists():
                shutil.rmtree(dest)
            _run_git(["clone", "--", source, str(dest)])
            # ``ref`` was validated above not to start with ``-``, so it can't be
            # mistaken for an option here.
            _run_git(["checkout", "--detach", ref], cwd=dest)
    else:
        _run_git(["clone", "--depth", "1", "--", source, str(dest)])
    sha = _run_git(["rev-parse", "HEAD"], cwd=dest)
    record: dict = {"type": "git", "url": source, "sha": sha}
    if ref:
        record["ref"] = ref
    return record


def _validated_manifest(plugin_dir: Path) -> PluginManifest:
    try:
        return load_manifest(plugin_dir)
    except ManifestError as exc:
        raise InstallError(str(exc)) from exc


def _materialize(src_dir: Path, dest: Path, *, link: bool) -> None:
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if link:
        dest.symlink_to(src_dir.resolve(), target_is_directory=True)
    else:
        shutil.copytree(src_dir, dest)


def install_plugin(
    source: str,
    *,
    scope: str,
    workspace_root,
    trust: bool,
    link: bool = False,
    name_override: str | None = None,
    now: str,
    _force_git: bool = False,
) -> InstalledPlugin:
    """Install ``source`` (a local dir or git URL) into ``scope``. Returns the
    written registry record."""
    target_root = scope_dir(scope, workspace_root)
    use_git = _force_git or is_git_source(source)

    source_record: dict
    with tempfile.TemporaryDirectory() as tmp:
        if use_git:
            if link:
                raise InstallError("--link is only valid for local sources")
            staging = Path(tmp) / "clone"
            source_record = _clone_git(source, staging, ref=None)
        else:
            staging = Path(source)
            if not staging.is_dir():
                raise InstallError(f"not a directory: {source}")
            source_record = {"type": "local", "path": str(staging.resolve())}

        manifest = _validated_manifest(staging)
        name = name_override or manifest.name
        # ``name`` is used below as a path component (``target_root / name``).
        # ``manifest.name`` was already validated by load_manifest, but a CLI
        # ``--name`` override bypasses that check, so a value like ``../../x``
        # would escape the scope dir. Refuse any non-identifier name here.
        if not valid_plugin_name(name):
            raise InstallError(f"invalid plugin name: {name!r}")
        summary = plugin_bundle_summary(manifest)
        is_linked = bool(link and not use_git)
        if is_linked:  # noqa: SIM108 — a flattened nested ternary would hurt readability
            # A linked plugin points at a live, mutable source dir, so the
            # executable surface read at discovery time can differ from what is
            # inspected here. Never auto-trust it: hooks/MCP added to the source
            # after install would otherwise run trusted with no prompt. Trust
            # must be granted explicitly for a linked install.
            trusted = bool(trust)
            # Record whether the source had executable parts (hooks/MCP) at the
            # moment trust was granted. Discovery re-checks the *live* source each
            # time and uses this baseline to detect post-trust elevation — a
            # linked plugin that later gains hooks/MCP must not run that newly
            # added code under the original trust without re-confirmation.
            source_record["executable_at_install"] = has_executable(summary)
        else:
            trusted = True if not has_executable(summary) else bool(trust)

        dest = target_root / name
        # For git, always copy (link only applies to local sources).
        _materialize(staging, dest, link=is_linked)

    record = InstalledPlugin(
        name=name,
        version=manifest.version,
        source=source_record,
        enabled=True,
        trusted=trusted,
        linked=is_linked,
        installed_at=now,
    )
    state = load_state(target_root)
    state[name] = record
    save_state(target_root, state)
    return record


def _mutate(name: str, scope: str, workspace_root, fn) -> bool:
    target_root = scope_dir(scope, workspace_root)
    state = load_state(target_root)
    rec = state.get(name)
    if rec is None:
        return False
    fn(rec)
    save_state(target_root, state)
    return True


def set_enabled(name: str, *, scope: str, workspace_root, enabled: bool) -> bool:
    def _apply(r):
        r.enabled = enabled
    return _mutate(name, scope, workspace_root, _apply)


def set_trusted(name: str, *, scope: str, workspace_root, trusted: bool) -> bool:
    def _apply(r):
        r.trusted = trusted
    return _mutate(name, scope, workspace_root, _apply)


def remove_plugin(name: str, *, scope: str, workspace_root) -> bool:
    # ``name`` reaches ``shutil.rmtree(target_root / name)`` below, so a traversal
    # value like ``../../../home/user/x`` would delete out of tree. load_state
    # already drops such names from the registry, but validate at this boundary
    # too so an invalid name is refused before any path is built from it.
    if not valid_plugin_name(name):
        return False
    target_root = scope_dir(scope, workspace_root)
    state = load_state(target_root)
    if name not in state:
        return False
    dest = target_root / name
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    del state[name]
    save_state(target_root, state)
    return True


def _has_executable_surface(plugin_dir: Path) -> bool:
    """Whether the plugin currently on disk ships hooks/MCP. Best-effort: an
    unreadable/invalid manifest is treated as inert, which is the safe default
    for the update trust check (it makes any newly-added executable surface look
    like an elevation and drop trust)."""
    try:
        manifest = load_manifest(plugin_dir)
    except ManifestError:
        return False
    return has_executable(plugin_bundle_summary(manifest))


def update_plugin(name: str, *, scope: str, workspace_root, now: str) -> InstalledPlugin:
    """Re-fetch a git-sourced plugin to the latest of its ref. Local/linked
    plugins cannot be updated this way."""
    # ``name`` is used as a path component (``target_root / name``); refuse a
    # traversal value before it can be joined onto the scope dir.
    if not valid_plugin_name(name):
        raise InstallError(f"invalid plugin name: {name!r}")
    target_root = scope_dir(scope, workspace_root)
    state = load_state(target_root)
    rec = state.get(name)
    if rec is None:
        raise InstallError(f"plugin not installed: {name}")
    if rec.source.get("type") != "git":
        raise InstallError(f"{name} was not installed from git; reinstall to update")
    url = rec.source["url"]
    ref = rec.source.get("ref")
    dest = target_root / name
    # Executable surface of the version on disk *before* we overwrite it. Used to
    # detect an update that introduces hooks/MCP into a previously inert plugin.
    had_executable = _has_executable_surface(dest)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "clone"
        source_record = _clone_git(url, staging, ref=ref)
        manifest = _validated_manifest(staging)
        now_executable = has_executable(plugin_bundle_summary(manifest))
        _materialize(staging, dest, link=False)
    rec.version = manifest.version
    rec.source = source_record
    rec.installed_at = now
    # Trust-elevation guard. An inert plugin is auto-trusted at install; if an
    # upstream update adds a hook or MCP server, that now-executable code would
    # otherwise run trusted with no prompt. Drop trust so it must be re-granted —
    # the same threat the linked-install path guards against (see install_plugin).
    if now_executable and not had_executable:
        rec.trusted = False
    save_state(target_root, state)
    return rec
