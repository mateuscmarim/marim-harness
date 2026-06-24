import os
from pathlib import Path


def config_dir() -> Path:
    """marim's per-user config directory ($XDG_CONFIG_HOME/marim, else
    ~/.config/marim)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "marim"


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
        for key, value in dotenv_values(project).items():
            if value is None or key in _PROJECT_ENV_BLOCKLIST:
                continue
            os.environ.setdefault(key, value)  # shell env still wins
    load_dotenv(global_config_path())  # global fallback (may set blocked keys)
