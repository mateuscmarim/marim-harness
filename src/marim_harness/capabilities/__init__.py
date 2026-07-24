"""Capabilities marim exports for use with ANY pydantic-ai agent.

These are standard pydantic-ai ``AbstractCapability`` implementations —
attach them via ``Agent(capabilities=[...])`` or marim's
``HarnessBuilder.with_capability``. They deliberately depend only on
pydantic-ai plus marim's pure helpers, never on marim's runtime (Deps,
services, TUI)."""

from .advisor import Advisor

__all__ = ["Advisor"]
