"""multilspy LanguageServer subclass for basedpyright.

multilspy hard-wires python -> jedi-language-server (its only bundled Python
server). jedi-language-server is in maintenance mode upstream, while
basedpyright — the pip-installable community fork of pyright — is actively
developed and ships the LSP surface pyright reserves for closed-source
Pylance. This subclass follows the exact shape of multilspy's own per-server
classes (a launch command plus initialize params plus a start_server
handshake); the manager's ``_default_factory`` prefers it whenever
``basedpyright-langserver`` is on PATH, keeping multilspy's jedi routing as
the fallback.

Handshake differences from multilspy's JediServer, both load-bearing:

- pyright issues ``workspace/configuration`` *requests* during startup; an
  unanswered request stalls the server, so a handler answering "no explicit
  settings" (one empty dict per queried item) is registered before start.
- there is no ``experimental/serverStatus`` readiness notification; the
  server is usable once ``initialized`` is acknowledged, so
  ``completions_available`` is set right after it (matching how multilspy's
  other non-jedi servers signal readiness).

multilspy is imported at module top — this module is itself imported lazily
(from ``_default_factory``, only when a python server actually starts), which
keeps the heavy dependency off the ``import marim_harness`` path.
"""

from __future__ import annotations

import logging
import os
import pathlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from multilspy.language_server import LanguageServer
from multilspy.lsp_protocol_handler.lsp_types import InitializeParams
from multilspy.lsp_protocol_handler.server import ProcessLaunchInfo
from multilspy.multilspy_config import MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger


class BasedPyrightServer(LanguageServer):
    """basedpyright over stdio, driven through multilspy's client plumbing."""

    def __init__(
        self, config: MultilspyConfig, logger: MultilspyLogger, repository_root_path: str
    ):
        super().__init__(
            config,
            logger,
            repository_root_path,
            ProcessLaunchInfo(cmd="basedpyright-langserver --stdio", cwd=repository_root_path),
            "python",
        )

    def _get_initialize_params(self, repository_absolute_path: str) -> InitializeParams:
        """Standard LSP initialize params. Capabilities stay minimal: pyright
        serves definition/references/documentSymbol/hover/publishDiagnostics
        (everything the nav tools and diagnostics-on-edit use) without any
        opt-in client capability, and multilspy's response handling accepts
        both the flat and hierarchical documentSymbol shapes."""
        root_uri = pathlib.Path(repository_absolute_path).as_uri()
        return cast(InitializeParams, {
            "processId": os.getpid(),
            "rootPath": repository_absolute_path,
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True},
                    "publishDiagnostics": {"versionSupport": True},
                },
                "workspace": {"workspaceFolders": True, "configuration": True},
            },
            "initializationOptions": {},
            "workspaceFolders": [
                {
                    "uri": root_uri,
                    "name": os.path.basename(repository_absolute_path),
                }
            ],
        })

    @asynccontextmanager
    async def start_server(self) -> AsyncGenerator[BasedPyrightServer, None]:
        async def workspace_configuration(params):
            # "No explicit settings" for every queried section: pyright then
            # applies its defaults. Leaving the request unanswered stalls it.
            return [{}] * len(params.get("items", []))

        async def do_nothing(params):
            return

        async def window_log_message(msg):
            self.logger.log(f"LSP: window/logMessage: {msg}", logging.INFO)

        self.server.on_request("workspace/configuration", workspace_configuration)
        self.server.on_request("client/registerCapability", do_nothing)
        self.server.on_request("window/workDoneProgress/create", do_nothing)
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("$/progress", do_nothing)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)

        async with super().start_server():
            self.logger.log("Starting basedpyright-langserver process", logging.INFO)
            await self.server.start()
            initialize_params = self._get_initialize_params(self.repository_root_path)
            await self.server.send.initialize(initialize_params)
            self.server.notify.initialized({})
            self.completions_available.set()

            yield self

            await self.server.shutdown()
            await self.server.stop()
