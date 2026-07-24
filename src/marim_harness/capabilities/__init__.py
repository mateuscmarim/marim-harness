"""Capabilities marim exports for use with ANY pydantic-ai agent.

These are standard pydantic-ai ``AbstractCapability`` implementations —
attach them via ``Agent(capabilities=[...])`` or marim's
``HarnessBuilder.with_capability``. They deliberately depend only on
pydantic-ai plus marim's pure helpers, with no *functional* dependency on
marim's runtime — they never touch ``Deps``, services, or TUI objects, even
though importing this package transitively loads some of those runtime
modules."""

from .advisor import Advisor

__all__ = ["Advisor"]
