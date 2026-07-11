"""The settings screen's Providers section: stacked cards for the four built-in
providers (openrouter / google / local / claude-cli), a default-provider radio,
live apply, implicit verification, and key removal.

Credentials save to the GLOBAL .env only (a project .env may not set these keys
at all — see _PROJECT_ENV_BLOCKLIST in config/env.py), and ``save_env_settings``
mirrors them into ``os.environ``, so an in-place
``MultiModelSource.refresh_from_env()`` right after a save makes the provider
active for the model picker without a restart. Key inputs are password fields
that start EMPTY — the placeholder proves the configured state without ever
painting the secret, and an empty commit is a no-op so focus/blur can never
clobber a stored key."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import Button, Input, RadioButton, RadioSet, Static

from ...config import MultiModelSource, save_env_settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from ...config.model import ModelSource

_KNOWN = ("openrouter", "google", "local", "claude-cli")


@dataclass(frozen=True)
class ProviderSpec:
    """Which env keys one provider reads/writes, driving its settings card."""

    name: str
    write_key: str | None  # env var an API-key commit writes (None: no key field)
    key_fallbacks: tuple[str, ...]  # alt env names probed for the placeholder hint
    read_keys: tuple[str, ...]  # any of these set ⇒ configured
    drop_keys: tuple[str, ...]  # removed together by the remove button
    base_url_key: str | None = None  # local only


PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "openrouter",
        write_key="OPENROUTER_API_KEY",
        key_fallbacks=(),
        read_keys=("OPENROUTER_API_KEY",),
        drop_keys=("OPENROUTER_API_KEY",),
    ),
    # google is configured by EITHER env name, but a save always writes
    # GOOGLE_API_KEY and a remove must drop BOTH (either one alone would
    # keep the provider configured).
    ProviderSpec(
        "google",
        write_key="GOOGLE_API_KEY",
        key_fallbacks=("GEMINI_API_KEY",),
        read_keys=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        drop_keys=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    ),
    # local is marked configured by its base URL (matching _provider_has_creds);
    # removal clears URL + key together — a leftover key alone is meaningless.
    ProviderSpec(
        "local",
        write_key="MARIM_API_KEY",
        key_fallbacks=(),
        read_keys=("MARIM_BASE_URL",),
        drop_keys=("MARIM_BASE_URL", "MARIM_API_KEY"),
        base_url_key="MARIM_BASE_URL",
    ),
    # claude-cli stores nothing: the CLI owns auth; status is binary detection.
    ProviderSpec(
        "claude-cli", write_key=None, key_fallbacks=(), read_keys=(), drop_keys=()
    ),
)
_SPECS = {s.name: s for s in PROVIDER_SPECS}

_DEFAULT_LOCAL_URL = "http://localhost:11434/v1"


def key_hint(value: str | None) -> str:
    """Placeholder for a password input: proves whether a key is stored — and
    shows its last 4 chars when the key is long enough that this reveals
    nothing useful — without ever painting the secret itself."""
    if not value:
        return "not set"
    if len(value) >= 8:
        return f"configured · …{value[-4:]} — type to replace"
    return "configured — type to replace"


def short_error(exc: Exception) -> str:
    """First line of an exception, truncated to fit the one-line card badge."""
    text = (str(exc) or type(exc).__name__).splitlines()[0]
    return text if len(text) <= 48 else text[:47] + "…"


def spec_configured(spec: ProviderSpec) -> bool:
    """Env-based configured check (any read key set). claude-cli has no read
    keys — the pane special-cases it via CLI-binary detection instead."""
    return any(os.getenv(k) for k in spec.read_keys)


def current_default_provider() -> str:
    """MARIM_PROVIDER from the env, normalized like load_config: lowercased,
    unknown values falling back to openrouter (the historical default)."""
    default = os.getenv("MARIM_PROVIDER", "openrouter").lower()
    return default if default in _KNOWN else "openrouter"


class ProvidersPane(Vertical):
    """Stacked provider cards + default-provider radio (the Providers section).

    All persistence goes through ``save_env_settings`` (global .env, mirrored
    into os.environ); ``_refresh_sources`` then mutates the live
    ``MultiModelSource`` in place so the model picker sees the change
    immediately — see the module docstring. ``model_source`` may be a plain
    ModelSource or None (embedding/tests): saving still works, only the live
    refresh + verification are skipped."""

    DEFAULT_CSS = """
    ProvidersPane { height: auto; }
    .prov-card { height: auto; margin-bottom: 1; }
    .prov-head { height: 1; }
    .prov-dot { width: 2; }
    .prov-name { width: 14; text-style: bold; }
    .prov-status { width: 1fr; color: $text-muted; }
    .prov-head Button { width: auto; height: 1; border: none; padding: 0 1; }
    .prov-field { height: 3; padding-left: 2; }
    .prov-field Static { width: 10; height: 3; content-align: left middle; color: $text-muted; }
    .prov-field Input { width: 48; }
    .prov-note { height: 1; padding-left: 2; color: $text-muted; }
    #prov-default-label { margin-top: 1; }
    """

    def __init__(
        self,
        *,
        model_source: object | None,
        status: Callable[[str], None],
        set_badge: Callable[[str], None],
        cli_detected: bool,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._model_source = model_source
        self._status = status
        self._set_badge = set_badge
        self._cli_detected = cli_detected
        # Gate commits until mounted: widget events fired while the initial
        # tree mounts (e.g. the RadioSet preselect) must not persist anything.
        self._ready = False

    def compose(self) -> ComposeResult:
        yield Static(
            "Credentials save to the global .env and apply immediately.",
            classes="muted",
        )
        for spec in PROVIDER_SPECS:
            yield from self._card(spec)
        yield Static("Default provider (new sessions)", id="prov-default-label")
        current = current_default_provider()
        with RadioSet(id="prov-default-set"):
            for spec in PROVIDER_SPECS:
                yield RadioButton(
                    spec.name,
                    value=(spec.name == current),
                    id=f"prov-default-{spec.name}",
                )

    def _card(self, spec: ProviderSpec) -> ComposeResult:
        name = spec.name
        with Vertical(id=f"prov-card-{name}", classes="prov-card"):
            with Horizontal(classes="prov-head"):
                yield Static("", id=f"prov-dot-{name}", classes="prov-dot")
                yield Static(name, classes="prov-name")
                yield Static("", id=f"prov-status-{name}", classes="prov-status")
                if spec.drop_keys:
                    yield Button("remove", id=f"prov-remove-{name}")
            if spec.base_url_key is not None:
                with Horizontal(classes="prov-field"):
                    yield Static("Base URL")
                    yield Input(
                        value=os.getenv(spec.base_url_key, ""),
                        placeholder=_DEFAULT_LOCAL_URL,
                        id=f"prov-url-{name}",
                    )
            if spec.write_key is not None:
                with Horizontal(classes="prov-field"):
                    yield Static("API key")
                    yield Input(password=True, id=f"prov-key-{name}")
            if name == "claude-cli":
                yield Static(
                    "(auth handled by the claude CLI itself)", classes="prov-note"
                )

    def on_mount(self) -> None:
        for spec in PROVIDER_SPECS:
            self._paint_card(spec)
            # Verify already-configured providers up front so the cards open
            # showing live truth ('✓ connected · N models'), matching what a
            # save would show — skipped when there's no MultiModelSource
            # (embedding/tests) and for claude-cli (nothing to fetch).
            if spec.write_key is not None and self._configured(spec):
                self._start_verify(spec.name)
        # call_after_refresh (not a bare assignment): the RadioSet's initial
        # Changed message may still be queued when on_mount runs; arming
        # commits only after the first refresh guarantees mount noise is over.
        self.call_after_refresh(self._arm)

    def _arm(self) -> None:
        self._ready = True

    # -- painting ----------------------------------------------------------

    def _configured(self, spec: ProviderSpec) -> bool:
        if spec.name == "claude-cli":
            return self._cli_detected
        return spec_configured(spec)

    def _paint_card(self, spec: ProviderSpec) -> None:
        name = spec.name
        configured = self._configured(spec)
        tv = self.app.theme_variables
        color = (
            tv.get("success", "#5fae7e")
            if configured
            else tv.get("text-muted", "#7c828d")
        )
        self.query_one(f"#prov-dot-{name}", Static).update(
            Content.assemble(("●" if configured else "○", color))
        )
        self.query_one(f"#prov-status-{name}", Static).update(
            self._status_text(spec, configured)
        )
        if spec.drop_keys:
            self.query_one(f"#prov-remove-{name}", Button).display = configured
        if spec.write_key is not None:
            self.query_one(f"#prov-key-{name}", Input).placeholder = key_hint(
                self._stored_key(spec)
            )

    def _status_text(self, spec: ProviderSpec, configured: bool) -> str:
        if spec.name == "claude-cli":
            base = "detected on PATH" if configured else "not found"
        else:
            base = "configured" if configured else "not configured"
        if spec.name == current_default_provider():
            base += " · default"
        return base

    def _stored_key(self, spec: ProviderSpec) -> str | None:
        """The stored credential the placeholder hints at: the canonical write
        key first, then fallbacks (google's GEMINI_API_KEY). Never read_keys —
        local's read key is its base URL, not a secret."""
        for key in (spec.write_key, *spec.key_fallbacks):
            value = os.getenv(key) if key else None
            if value:
                return value
        return None

    # -- persistence -------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._commit(event.input.id or "")

    def on_input_blurred(self, event: Input.Blurred) -> None:
        event.stop()
        self._commit(event.input.id or "")

    def _spec_for_input(self, widget_id: str) -> ProviderSpec | None:
        for prefix in ("prov-key-", "prov-url-"):
            if widget_id.startswith(prefix):
                return _SPECS.get(widget_id.removeprefix(prefix))
        return None

    def _commit(self, widget_id: str) -> None:
        if not self._ready:
            return
        spec = self._spec_for_input(widget_id)
        if spec is None:
            return
        env_key = (
            spec.base_url_key if widget_id.startswith("prov-url-") else spec.write_key
        )
        if env_key is None:
            return
        inp = self.query_one(f"#{widget_id}", Input)
        value = inp.value.strip()
        if not value:
            return  # empty commit is a no-op: focus/blur can't clobber a key
        if not self._save({env_key: value}):
            return
        if inp.password:
            inp.value = ""  # never leave the secret sitting in the widget
        self._status(f"✓ saved {env_key}")
        self._after_change(spec, verify=True)

    def _save(self, values: dict[str, str], *, drop: tuple[str, ...] = ()) -> bool:
        try:
            save_env_settings(values, drop=drop)
        except Exception as exc:  # surface any write failure on the status line
            self._status(f"Save failed: {exc}")
            return False
        return True

    def _after_change(self, spec: ProviderSpec, *, verify: bool = False) -> None:
        self._refresh_sources()
        self._paint_card(spec)
        if verify:
            self._start_verify(spec.name)

    def _refresh_sources(self) -> None:
        if isinstance(self._model_source, MultiModelSource):
            self._model_source.refresh_from_env()

    def _provider_source(self, name: str) -> ModelSource | None:
        if isinstance(self._model_source, MultiModelSource):
            return self._model_source.sources.get(name)
        return None

    # -- verification ------------------------------------------------------

    def _start_verify(self, name: str) -> None:
        """Fire-and-forget catalog fetch for one provider. exclusive per-group:
        a re-save while a fetch is in flight cancels the stale one so badges
        can't arrive out of order."""
        if self._provider_source(name) is None:
            return
        self.run_worker(self._verify(name), group=f"verify-{name}", exclusive=True)

    async def _verify(self, name: str) -> None:
        source = self._provider_source(name)
        if source is None:
            return
        badge = self.query_one(f"#prov-status-{name}", Static)
        default = " · default" if name == current_default_provider() else ""
        badge.update(f"verifying…{default}")
        try:
            models = await source.list_models()
        except Exception as exc:  # noqa: BLE001 - any fetch failure is a verdict
            badge.update(f"✗ {short_error(exc)}{default}")
            return
        badge.update(f"✓ connected · {len(models)} models{default}")

    # -- removal -----------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        if bid.startswith("prov-remove-"):
            self._remove(bid.removeprefix("prov-remove-"))

    def _remove(self, name: str) -> None:
        """Drop a provider's stored credentials — .env line(s) and os.environ
        in one save_env_settings call. No confirmation modal: a deliberate
        button click in a personal tool, confirmed on the footer. The running
        session's model keeps working (the harness holds the built instance);
        the next switch or session routes to the default provider."""
        spec = _SPECS[name]
        if not spec.drop_keys or not self._save({}, drop=spec.drop_keys):
            return
        if spec.base_url_key is not None:
            self.query_one(f"#prov-url-{name}", Input).value = ""
        self._status(f"✓ removed {name} credentials")
        self._after_change(spec)
