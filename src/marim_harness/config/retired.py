"""Migration guidance for persisted selections of removed execution backends."""

CODEX_CLI_REMOVED = (
    "codex-cli has been removed. Select openai-codex:<model> for native Codex "
    "subscription access (or set MARIM_PROVIDER=openai-codex and MARIM_MODEL). "
    "For sub-agents, use backend: native with an openai-codex model override. "
    "Old CLI transcripts remain readable, but CLI threads cannot be resumed."
)


def reject_codex_cli(value: str | None) -> None:
    if value and value.partition(":")[0].lower() == "codex-cli":
        raise ValueError(CODEX_CLI_REMOVED)
