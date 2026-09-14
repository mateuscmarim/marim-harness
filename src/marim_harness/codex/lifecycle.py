"""Public app-server lifecycle translation, scoped to a turn translator."""

from __future__ import annotations

from collections import defaultdict

from .translate import Notice


def _text(obj: dict, key: str) -> str:
    value = obj.get(key)
    return value if isinstance(value, str) else ""


class CodexLifecycle:
    def __init__(self) -> None:
        self._items: set[tuple[str, str]] = set()
        self._modern: dict[str, int] = defaultdict(int)
        self._legacy: dict[str, int] = defaultdict(int)

    def translate(self, method: str, params: dict) -> list[object] | None:
        if method == "model/rerouted":
            old, new = _text(params, "fromModel"), _text(params, "toModel")
            if not old or not new:
                return []
            return [
                Notice(
                    f"Codex model rerouted: {old} → {new}",
                    kind="model_rerouted",
                    data={k: _text(params, k) for k in ("fromModel", "toModel", "reason")},
                )
            ]
        if method in ("warning", "guardianWarning", "configWarning", "deprecationNotice"):
            key = "summary" if method in ("configWarning", "deprecationNotice") else "message"
            message = _text(params, key)
            return [Notice(message)] if message.strip() else []
        if method == "thread/compacted":
            return self._compacted(params, None)
        if method in ("item/started", "item/completed"):
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "contextCompaction":
                if method == "item/started":
                    return []
                return self._compacted(params, _text(item, "id"))
        return None

    def _compacted(self, params: dict, item_id: str | None) -> list[object]:
        turn = _text(params, "turnId")
        if item_id is not None:
            key = (turn, item_id)
            if not item_id or key in self._items:
                return []
            self._items.add(key)
            self._modern[turn] += 1
            fresh = self._modern[turn] > self._legacy[turn]
        else:
            self._legacy[turn] += 1
            fresh = self._legacy[turn] > self._modern[turn]
        if not fresh:
            return []
        return [Notice("Codex compacted its context", kind="compaction", severity="info")]
