"""A generic multilspy LanguageServer subclass for plugin-declared servers.

Third-party LSP plugins declare a launch command; this subclass runs it over
stdio and reuses all of multilspy's client plumbing (framing, initialize
handshake, request_definition/references/document_symbols, publishDiagnostics
collection). It mirrors BasedPyrightServer's handshake — answer
workspace/configuration so servers that request settings don't stall, signal
readiness once ``initialized`` is acked — with sensible defaults for a server
we know nothing specific about.

Imported lazily from the manager's factory, so multilspy stays off the
``import marim_harness`` path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import cast

from multilspy.language_server import LanguageServer
from multilspy.lsp_protocol_handler.lsp_types import InitializeParams
from multilspy.lsp_protocol_handler.server import ProcessLaunchInfo
from multilspy.multilspy_config import MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger

from .provider import LspProvider

logger = logging.getLogger(__name__)


class GenericStdioServer(LanguageServer):
    """Any LSP server over stdio, driven through multilspy's client plumbing."""

    def __init__(
        self,
        config: MultilspyConfig,
        logger_: MultilspyLogger,
        repository_root_path: str,
        *,
        cmd: str,
        language_id: str,
        env: dict[str, str] | None = None,
    ):
        launch = ProcessLaunchInfo(
            cmd=cmd, cwd=repository_root_path, env={**os.environ, **(env or {})}
        )
        super().__init__(config, logger_, repository_root_path, launch, language_id)
        self._launch_cmd = cmd  # exposed for tests / debugging

    @classmethod
    def from_provider(cls, provider: LspProvider, root: Path) -> GenericStdioServer:
        assert provider.command is not None  # declarative providers only
        cmd = " ".join([provider.command, *provider.args]).strip()
        config = MultilspyConfig.from_dict({"code_language": provider.language})
        return cls(
            config,
            MultilspyLogger(),
            str(root),
            cmd=cmd,
            language_id=provider.language,
            env=dict(provider.env),
        )

    def _get_initialize_params(self, repository_absolute_path: str) -> InitializeParams:
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
                {"uri": root_uri, "name": os.path.basename(repository_absolute_path)}
            ],
        })

    @asynccontextmanager
    async def start_server(self) -> AsyncGenerator[GenericStdioServer, None]:
        async def workspace_configuration(params):
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
            self.logger.log(f"Starting generic LSP process: {self._launch_cmd}", logging.INFO)
            await self.server.start()
            init_params = self._get_initialize_params(self.repository_root_path)
            await self.server.send.initialize(init_params)
            self.server.notify.initialized({})
            self.completions_available.set()
            yield self
            await self.server.shutdown()
            await self._await_process_exit()
            await self.server.stop()

    async def _await_process_exit(self) -> None:
        """Give asyncio's child-watcher time to reap the process ``shutdown()``
        just told to exit, before ``stop()`` inspects it via psutil.

        multilspy's ``shutdown()`` sends the LSP ``exit`` notification and then
        yields the loop exactly once (``await asyncio.sleep(0)``). For a
        server that dies within microseconds of receiving that notification —
        our test fixture, and plausibly any lightweight plugin server — that
        single yield is not enough for asyncio to notice the child has
        exited, so ``process.returncode`` is still ``None`` when ``stop()``
        runs. ``stop()`` then falls into
        ``_terminate_or_kill_process``/``_signal_process_tree``, which calls
        ``psutil.Process(pid).is_running()`` and then unconditionally
        ``.children()`` on it: a TOCTOU race, since ``children()`` internally
        re-reads the process's create_time, and if the process gets reaped in
        between, it raises ``psutil.NoSuchProcess`` — a real bug in
        multilspy, since that call isn't guarded like its sibling calls in
        the same method. Actually awaiting the process's exit here lets
        asyncio's own machinery record it first, so ``stop()`` sees an
        already non-``None`` returncode and skips the racy psutil path
        entirely. Bounded by a short timeout so a server that ignores
        ``exit`` doesn't hang teardown — ``stop()`` still force-kills it.
        """
        process = self.server.process
        if process is None:
            return
        with suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
