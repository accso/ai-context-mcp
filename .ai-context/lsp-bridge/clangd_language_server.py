"""
clangd_language_server.py — a from-scratch multilspy-compatible
`LanguageServer` subclass for C/C++, backed by LLVM's own `clangd`.

Why this file exists (not `arm64_lsp_overrides.py`): multilspy 0.0.15 has
**no C/C++ support at all** — its `Language` enum only has
csharp/python/rust/java/kotlin/typescript/javascript/go/ruby/dart (verified
live in the installed package). Unlike every other language shipped so far
(which needed at most a small linux-arm64 manifest fixup on top of
multilspy's own dispatch), C/C++ needs a genuine new subclass, written the
same way multilspy itself writes its per-language classes (see
`multilspy/language_servers/gopls/gopls.py`, the closest existing template:
no manifest/download logic, just shells out to a binary already on `PATH`).

Licensing: `clangd` is part of LLVM and distributed under
"Apache-2.0 WITH LLVM-exception" — see "Licensing" in
docs/lsp-integration.md for why this shows up as
`NOASSERTION` on GitHub (the SPDX combination isn't a single recognized
identifier) despite being *more* permissive than plain Apache-2.0, not
less (the LLVM exception only adds permissions, e.g. static-linking
object-code exception; it removes none of Apache-2.0's own permissions).
This bridge installs the official Debian `clangd` package unmodified and
only ever launches it as a subprocess over stdio, exactly like every other
language server here.

Architecture: unlike Rust/C#/Java/Dart (which download a pinned upstream
release .zip/.vsix at prewarm time), `clangd` is installed once at
build time via `apt-get install clangd` (see Dockerfile) — Debian
publishes native arm64 builds for every package in its main archive, so
there is no linux-arm64 gap to work around at all (verified: `clangd
--version` runs cleanly under `--platform linux/arm64`). This makes C/C++
the simplest rollout after Go/Ruby: no manifest, no download, no
arm64-specific code path needed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import subprocess
from contextlib import asynccontextmanager
from typing import AsyncIterator

from multilspy.language_server import LanguageServer
from multilspy.lsp_protocol_handler.lsp_types import InitializeParams
from multilspy.lsp_protocol_handler.server import ProcessLaunchInfo
from multilspy.multilspy_config import MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger

# The `code_language` value used for this language in config.yaml /
# validate_config.py. Deliberately NOT a multilspy `Language` enum member
# (no such member exists) — main.py special-cases this string instead of
# doing a `Language(language)` enum lookup for it.
CPP_LANGUAGE_ID = "cpp"


class ClangdLanguageServer(LanguageServer):
    """
    C/C++ support via LLVM's `clangd`, installed at build time (see
    Dockerfile). Modeled directly on multilspy's own `Gopls` class — the
    simplest existing template, since `clangd` (like `gopls`) needs no
    project-specific download/manifest handling, just a binary on `PATH`.
    """

    @staticmethod
    def _get_clangd_version() -> str | None:
        try:
            result = subprocess.run(["clangd", "--version"], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
        except FileNotFoundError:
            return None
        return None

    @classmethod
    def setup_runtime_dependency(cls) -> bool:
        version = cls._get_clangd_version()
        if not version:
            raise RuntimeError(
                "Missing required dependency: clangd. Install via "
                "`apt-get install clangd` (Debian/Ubuntu) or see "
                "https://clangd.llvm.org/installation for other platforms."
            )
        return True

    def __init__(self, config: MultilspyConfig, logger: MultilspyLogger, repository_root_path: str):
        self.setup_runtime_dependency()

        super().__init__(
            config,
            logger,
            repository_root_path,
            ProcessLaunchInfo(cmd="clangd", cwd=repository_root_path),
            CPP_LANGUAGE_ID,
        )
        self.server_ready = asyncio.Event()

    def _get_initialize_params(self, repository_absolute_path: str) -> InitializeParams:
        root_uri = pathlib.Path(repository_absolute_path).as_uri()
        return {
            "processId": os.getpid(),
            "locale": "en",
            "rootPath": repository_absolute_path,
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True, "dynamicRegistration": True},
                    "completion": {
                        "dynamicRegistration": True,
                        "completionItem": {"snippetSupport": True},
                    },
                    "definition": {"dynamicRegistration": True},
                    "references": {"dynamicRegistration": True},
                    "hover": {"dynamicRegistration": True},
                },
                "workspace": {
                    "workspaceFolders": True,
                    "didChangeConfiguration": {"dynamicRegistration": True},
                },
            },
            "workspaceFolders": [
                {
                    "uri": root_uri,
                    "name": os.path.basename(repository_absolute_path),
                }
            ],
        }

    @asynccontextmanager
    async def start_server(self) -> AsyncIterator["ClangdLanguageServer"]:
        async def register_capability_handler(params):
            return

        async def window_log_message(msg):
            self.logger.log(f"LSP: window/logMessage: {msg}", logging.INFO)

        async def do_nothing(params):
            return

        self.server.on_request("client/registerCapability", register_capability_handler)
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("$/progress", do_nothing)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)

        async with super().start_server():
            self.logger.log("Starting clangd server process", logging.INFO)
            await self.server.start()
            initialize_params = self._get_initialize_params(self.repository_root_path)

            self.logger.log(
                "Sending initialize request from LSP client to LSP server and awaiting response",
                logging.INFO,
            )
            init_response = await self.server.send.initialize(initialize_params)

            assert "textDocumentSync" in init_response["capabilities"]
            assert "definitionProvider" in init_response["capabilities"]

            self.server.notify.initialized({})
            self.completions_available.set()

            self.server_ready.set()
            await self.server_ready.wait()

            yield self

            await self.server.shutdown()
            await self.server.stop()
