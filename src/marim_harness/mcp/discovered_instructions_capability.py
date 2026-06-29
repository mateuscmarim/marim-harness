"""Deliver a discovered MCP server's ``instructions`` from the *cacheable message
history* rather than an uncached dynamic instruction (V1). A custom capability
appends the guidance once, at the moment of discovery, so it lands in the same
request where the model first uses the discovered tools and is a cache-read on
every request thereafter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.tools import AgentDepsT, RunContext

from .catalog import cap_instructions

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import ModelRequestContext

    from .manager import McpManager

# A per-server sentinel placed in the synthetic ModelResponse so prior injection is
# detectable by scanning history (guillemets keep it clear of normal prose).
_MARKER_RE = re.compile(r"«mcp-guidance:([^»]+)»")


def _marker(server: str) -> str:
    return f"«mcp-guidance:{server}»"


def _envelope(server: str, text: str) -> str:
    # Labelled so the model reads it as server-authored guidance, not a user utterance.
    return (
        f'[MCP server "{server}" — usage guidance; follow it for that '
        f"server's tools]\n{text}"
    )


def _instruction_messages(server: str, text: str) -> list[ModelMessage]:
    # ModelResponse(marker) + ModelRequest(envelope): the pair ends in a ModelRequest
    # (required: request_context.messages[-1] must be a ModelRequest) and carries no
    # tool-call parts, so it cannot create an unanswered-ToolCallPart resumability hazard.
    return [
        ModelResponse(parts=[TextPart(_marker(server))]),
        ModelRequest(parts=[UserPromptPart(_envelope(server, cap_instructions(text)))]),
    ]


def _injected_servers(messages: list[ModelMessage]) -> set[str]:
    """Servers already injected this session, found by scanning history for the
    per-server marker. Re-derived every call so it self-heals across resume (fresh
    capability instance) and compaction (marker summarised away → re-inject)."""
    out: set[str] = set()
    for m in messages:
        for p in getattr(m, "parts", []):
            content = getattr(p, "content", None)
            if isinstance(content, str):
                out.update(_MARKER_RE.findall(content))
    return out


@dataclass
class DiscoveredInstructionsCapability(AbstractCapability[AgentDepsT]):
    """Inject discovered MCP servers' instructions into cacheable history, once each.

    Fires after pydantic-ai refreshes ``ctx.discovered_tool_names`` from history, so a
    server discovered this run is injected on the same request the model first uses it."""

    mcp: McpManager

    async def before_model_request(
        self, ctx: RunContext[AgentDepsT], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        discovered = getattr(ctx, "discovered_tool_names", None) or set()
        if not discovered:
            return request_context
        already = _injected_servers(request_context.messages)
        for server, text in self.mcp.discovered_server_instructions(discovered):
            if server not in already:
                request_context.messages.extend(_instruction_messages(server, text))
        return request_context
