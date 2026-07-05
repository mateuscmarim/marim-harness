"""Implementation engines behind the thin tool wrappers.

The framework-free machinery the ``*_tools`` wrappers call into: filesystem
primitives (``fs``), shell execution (``shell``), URL fetch and web search
(``fetch``, ``web``), large-output offload (``offload``), and the tool-arg
coercion / unknown-tool suggestion helpers (``coerce``, ``suggest``). These are
pure(ish), side-effect-scoped, and unit-tested directly — the ``RunContext[Deps]``
wiring lives one level up in ``tools/*_tools.py`` (see CLAUDE.md's thin-tool-layer
vs pure-helper split).
"""
