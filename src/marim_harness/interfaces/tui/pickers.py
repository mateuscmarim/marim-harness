"""The three model-ish pickers reachable from the main screen (/model,
/advisor, /think) and the vision-capability cache that rides with them.

Grouped because they are the same shape three times over — open a modal, apply
the dismissal to a harness seam, echo the new value into the log — and because
the capability cache is a by-product of the model catalog those pickers already
fetch. The settings screen has its own copies: they persist to .env for *new*
sessions, whereas these apply to the running one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .model_picker import ModelPickerModal
from .thinking_picker import ThinkingPickerModal
from .widgets import NoticeMessage

if TYPE_CHECKING:
    from .app import HarnessApp

logger = logging.getLogger(__name__)


class ModelPickers:
    """Opens the live pickers and applies their results to the harness."""

    def __init__(self, app: HarnessApp) -> None:
        self._app = app
        # Qualified model id -> supports_images, or absent/None when unknown.
        # Seeded in the background at startup and refreshed whenever the model
        # catalog is fetched, so the text-only-model warning can fire before the
        # user has ever opened the picker.
        self.vision_caps: dict[str, bool | None] = {}

    async def open_model(self) -> None:
        """Open the picker and let the user choose a model, applying the choice to
        the harness. The catalog loads inside the modal's own worker, so the
        picker appears instantly even on a slow provider; it degrades to free-text
        when no catalog loads.

        Uses the callback form of push_screen (not push_screen_wait) so it works
        when called straight from the command-dispatch path, which is not a
        worker — push_screen_wait would raise NoActiveWorker there.
        """
        source = self._app.harness.model_source
        if source is None:
            await self._app.post_system("Model switching isn't available here.")
            return
        self._app.run_worker(
            self.refresh_vision_caps(source.list_models), exclusive=False
        )
        self._app.push_screen(
            ModelPickerModal(
                current=self._app.harness.model_id,
                fetch=source.list_models,
                is_local=source.is_local,
            ),
            self.on_model_chosen,
        )

    def on_model_chosen(self, chosen: str | None) -> None:
        """Apply a model selected in the picker. Invoked by push_screen when the
        modal is dismissed; a None result (cancelled) is a no-op."""
        if not chosen:
            return
        self._app.harness.set_model(chosen)
        self._app.status.model_name = self._app.harness.model_label
        self._app.append_log(
            NoticeMessage(f"model: {self._app.harness.model_label}")
        )

    async def open_advisor(self) -> None:
        """Model picker for the advisor. Mirrors open_model, but the choice lands
        on the advisor seam (session-persisted) rather than the live turn model."""
        source = self._app.harness.model_source
        if source is None:
            await self._app.post_system("Model switching isn't available here.")
            return
        self._app.push_screen(
            ModelPickerModal(
                current=self._app.harness.advisor_model_id,
                fetch=source.list_models,
                is_local=source.is_local,
            ),
            self.on_advisor_chosen,
        )

    def on_advisor_chosen(self, chosen: str | None) -> None:
        if not chosen:
            return
        # A typed "off" in the free-text picker means "disable", same as
        # `/advisor off` and the settings picker — map it to None (the seam's
        # off state), never persist the literal "off" as a model id (which
        # would leave the seam active and every consult failing to build it).
        if chosen.strip().lower() == "off":
            self._app.harness.set_advisor_model(None)
            self._app.append_log(NoticeMessage("advisor: off"))
            return
        self._app.harness.set_advisor_model(chosen)
        self._app.append_log(NoticeMessage(f"advisor: {chosen}"))

    async def open_thinking(self) -> None:
        """Fixed-list picker for the session thinking level. The choice lands
        on Harness.set_thinking_level (session-persisted, live)."""
        self._app.push_screen(
            ThinkingPickerModal(current=self._app.harness.thinking_level_id),
            self.on_thinking_chosen,
        )

    def on_thinking_chosen(self, chosen: str | None) -> None:
        if not chosen:
            return
        self._app.harness.set_thinking_level(chosen)
        self._app.append_log(NoticeMessage(f"thinking: {chosen}"))

    async def refresh_vision_caps(self, fetch) -> None:
        try:
            entries = await fetch()
        except Exception as exc:
            logger.debug("failed to refresh vision capabilities: %s", exc, exc_info=True)
            return  # unknown stays unknown; never blocks submit
        self.vision_caps = {e.qualified: e.supports_images for e in entries}

    def image_block_reason(self, attachments) -> str | None:
        """A warning to show instead of submitting, or None to proceed. Only a
        positive text-only capability blocks; unknown always proceeds."""
        if not attachments:
            return None
        model_id = self._app.harness.model_id
        if model_id is not None and self.vision_caps.get(model_id) is False:
            return (f"{model_id} can't read images — "
                    "switch to a vision model with /model or remove the image.")
        return None
