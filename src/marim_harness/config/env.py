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


def load_environment() -> None:
    """Populate the environment for a run. Loads the project-local .env (cwd and
    parents) first, then the global config as a fallback. python-dotenv never
    overrides an already-set variable, so precedence is: real shell env, then
    the project .env, then the global config."""
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))  # project-local, if any
    load_dotenv(global_config_path())  # global fallback
