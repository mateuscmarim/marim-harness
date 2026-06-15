import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.usage import RunUsage


def _default_base_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "marim-harness" / "sessions"


class SessionStore:
    """Persists one conversation per workspace to a JSON file in a central data
    directory, so a session can be resumed across launches."""

    def __init__(self, workspace_root, base_dir: Optional[Path] = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self._base = Path(base_dir) if base_dir is not None else _default_base_dir()
        digest = hashlib.sha256(str(self.workspace_root).encode()).hexdigest()[:12]
        self.path = self._base / f"{self.workspace_root.name}-{digest}.json"

    def save(self, history: list, usage: RunUsage) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "workspace": str(self.workspace_root),
            "tokens": {
                "input": usage.input_tokens,
                "output": usage.output_tokens,
            },
            "messages": json.loads(ModelMessagesTypeAdapter.dump_json(history)),
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.path)  # atomic swap so a crash mid-write can't corrupt

    def load(self) -> tuple[list, RunUsage]:
        if not self.path.exists():
            return [], RunUsage()
        data = json.loads(self.path.read_text())
        messages = ModelMessagesTypeAdapter.validate_python(data.get("messages", []))
        tok = data.get("tokens", {})
        usage = RunUsage(
            input_tokens=tok.get("input", 0),
            output_tokens=tok.get("output", 0),
        )
        return messages, usage

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
