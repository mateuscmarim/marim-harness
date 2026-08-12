# `marim update` command — design

Date: 2026-08-12
Status: draft

## Purpose

Add a `marim update` CLI command that upgrades the installed `marim-harness`
package to the latest version. A `--check` flag reports whether an update is
available without installing.

## CLI surface

```
marim update              # upgrade to latest version
marim update --check      # report if a newer version exists (no install)
```

## Architecture

New module `src/marim_harness/interfaces/cli/update_cmd.py`, following the
existing pattern (`import_cmd.py`, `trust_cmd.py`): exposes a `main(argv, *,
out, err)` that parses args, does the work, and returns an exit code.

Register `"update"` in `router.py`'s `_MANAGEMENT` set alongside the other
keywords. No module-name override needed — the keyword and module name match.

### Internal helpers

**`_check_latest()`** — hits `https://pypi.org/pypi/marim-harness/json` via
`httpx` (already a dependency), extracts `info.version`, and compares to
`importlib.metadata.version("marim-harness")`. Returns a small dataclass with
`current`, `latest`, `is_outdated`, and `release_url`.

**`_do_upgrade()`** — tries `uv tool upgrade marim-harness` first (subprocess);
if the exit code suggests it wasn't a `uv tool` install, falls back to `pip
install --upgrade marim-harness`. Streams stdout/stderr so the user sees
progress. Returns the subprocess exit code.

### `main()` flow

1. Parse args (argparse, `--check` flag only).
2. If `--check`: call `_check_latest()`, print result, exit.
3. Otherwise: call `_do_upgrade()`, print result, exit with subprocess code.

## Error handling

| Scenario | Behavior |
|---|---|
| PyPI unreachable on `--check` | Print "Could not reach PyPI to check for updates.", exit 1 |
| Already up to date | Print "marim-harness X.Y.Z is already the latest version.", exit 0 |
| `uv tool upgrade` not applicable | Suppress its stderr, fall through to `pip install --upgrade` |
| `pip install` fails | Let subprocess output flow to terminal, exit with its return code |
| Not installed as a package | Catch `PackageNotFoundError`, print hint, exit 1 |
| Successful upgrade | Print new version, exit 0 |

## Dependencies

No new dependencies. `httpx` and `importlib.metadata` are already in the
project tree.

## Testing

Unit tests in `tests/test_update_cmd.py`, using `unittest.mock` (already used
throughout the suite). The `main(argv, *, out, err)` seam lets tests capture
output without touching real streams.

| Test case | What's mocked | Expected |
|---|---|---|
| `--check` reports outdated | httpx returns newer version | exit 0, "newer version available" message |
| `--check` reports current | httpx returns same version | exit 0, "already the latest" message |
| `--check` with PyPI unreachable | httpx raises network error | exit 1, user-friendly message |
| `update` with `uv tool` success | subprocess returns 0 | correct command invoked, exit 0 |
| `update` falls back to pip | `uv tool` exits 1, pip succeeds | pip invoked, exit 0 |
| `update` with pip failure | both fail | exit code propagated |
| Package not installed | `version()` raises `PackageNotFoundError` | exit 1, hint message |
| Already latest, bare `update` | version check shows current == latest | "already the latest", no subprocess |
