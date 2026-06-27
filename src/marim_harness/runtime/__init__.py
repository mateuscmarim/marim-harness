"""The turn-execution runtime: the engine that drives one user turn to completion.

This package holds the harness, the run_turn → approval-loop → persist pipeline,
and the context/vocabulary they run against:

- ``harness``      — :class:`Harness`, ``HarnessConfig``, ``build_collaborators``,
                     ``build_services`` (the deps/services late-binding lives here).
- ``controller``   — :class:`TurnController`, the approval/persist loop.
- ``context``      — pure per-turn context-injection helpers.
- ``deps``         — :class:`Deps`, ``HarnessServices`` (the one contained cycle).
- ``permissions``  — :class:`Mode` and ``resolve_approvals``.
- ``errors``       — provider-error surfacing.
- ``instructions`` — system-prompt / instructions assembly.
- ``bootstrap``    — ``build_harness``, the composition root both front-ends call.

Imports target submodules directly (``from .harness import Harness``) rather than
this package root, matching the codebase's house style and keeping the
Deps ↔ HarnessServices cycle from leaking through ``__init__`` at import time.
"""
