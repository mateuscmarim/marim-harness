import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Numeric MARIM_* knobs that must be a positive integer. A non-int or non-positive
# value (negative/zero) is dropped from the environment during load so the
# downstream reader falls back to its built-in default instead of, e.g., sizing a
# context window to a garbage value. The fix lives here (not at the read site) so
# a hostile project .env can't smuggle a bad value past validation.
_POSITIVE_INT_KEYS = frozenset(
    {
        "MARIM_MAX_CONTEXT_TOKENS",
        "MARIM_WAKE_DEPTH_CAP",
        "MARIM_SUBAGENT_TRANSCRIPT_CAP",
    }
)


def config_dir() -> Path:
    """marim's per-user config directory ($XDG_CONFIG_HOME/marim, else
    ~/.config/marim)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "marim"


def builtin_root() -> Path:
    """The package's bundled skills/agents directory
    (``src/marim_harness/builtin``), shipped inside the wheel. Skills and agents
    discovered here are marim's own defaults; project/global roots shadow them."""
    return Path(__file__).resolve().parent.parent / "builtin"


def global_config_path() -> Path:
    """The global .env loaded as a fallback when run outside the project."""
    return config_dir() / ".env"


# Security-relevant settings that a project-local .env may NOT set. A cloned,
# untrusted repo ships its own .env; if it could flip MARIM_TRUST_PROJECT_HOOKS
# it would self-grant execution of its own .marim/hooks.json (arbitrary commands),
# and if it could rewrite the command allow/deny lists it could disarm the shell
# policy. These keys are honored only from the real shell environment or the
# user's global config — never from a project file.
_PROJECT_ENV_BLOCKLIST = frozenset(
    {
        "MARIM_TRUST_PROJECT_HOOKS",
        "MARIM_COMMAND_DENYLIST",
        "MARIM_COMMAND_ALLOWLIST",
    }
)


def load_environment() -> None:
    """Populate the environment for a run. Loads the project-local .env (cwd and
    parents) first, then the global config as a fallback. An already-set variable
    is never overridden, so precedence is: real shell env, then the project .env,
    then the global config — except for the security keys in
    ``_PROJECT_ENV_BLOCKLIST``, which the project .env is not allowed to set at
    all (those come only from the shell env or the trusted global config)."""
    from dotenv import dotenv_values, find_dotenv, load_dotenv

    project = find_dotenv(usecwd=True)  # project-local, if any
    if project:
        # The project .env comes from a possibly-cloned, untrusted repo and runs
        # on every startup. A corrupt or hostile file must never crash the process
        # before logging is even useful — fail soft (warn + continue), matching the
        # codebase rule that a broken file can't break a turn.
        try:
            project_values = dotenv_values(project)
        except Exception as exc:  # noqa: BLE001 - any parse failure is non-fatal
            logger.warning("Ignoring unreadable project .env at %s: %s", project, exc)
            project_values = {}
        for key, value in project_values.items():
            if value is None or key in _PROJECT_ENV_BLOCKLIST:
                continue
            os.environ.setdefault(key, value)  # shell env still wins
    load_dotenv(global_config_path())  # global fallback (may set blocked keys)
    _sanitize_positive_ints()


def _sanitize_positive_ints() -> None:
    """Drop any ``_POSITIVE_INT_KEYS`` env var whose value isn't a positive
    integer. Removing it (rather than rewriting it) lets the downstream reader's
    own default apply. Logs a warning so a typo'd or hostile value is visible."""
    for key in _POSITIVE_INT_KEYS:
        raw = os.environ.get(key)
        if raw is None:
            continue
        try:
            parsed = int(raw.strip())
        except ValueError:
            logger.warning("Ignoring invalid %s=%r (not an integer); using default.", key, raw)
            del os.environ[key]
            continue
        if parsed <= 0:
            logger.warning("Ignoring invalid %s=%r (must be positive); using default.", key, raw)
            del os.environ[key]
