"""
swift_language_server.py — a from-scratch multilspy-compatible
`LanguageServer` subclass for Swift, backed by Apple's own
`sourcekit-lsp` (bundled with the official Swift toolchain).

Why this file exists (not `arm64_lsp_overrides.py`): exactly like C/C++
(`clangd_language_server.py`), multilspy 0.0.15 has **no Swift support at
all** — its `Language` enum has no Swift member and no dispatch class
exists for it. This needs a genuine new subclass, not a manifest fixup —
modeled on the same template as `clangd_language_server.py`/multilspy's
own `Gopls`: no download/manifest logic inside the class itself, just
shells out to a binary already on `PATH` (the toolchain itself is
installed once at build time in the Dockerfile, not per-request).

Licensing: `sourcekit-lsp` is Apple's own first-party server, plain
**Apache-2.0** (confirmed by fetching LICENSE.txt directly from
apple/sourcekit-lsp — no LLVM-exception wrinkle like clangd). No
licensing-exception sign-off needed, same as clangd.

Architecture / arm64: Apple publishes an **official Debian 12 (bookworm)
aarch64 toolchain tarball** for every stable Swift release (verified: ran
`sourcekit-lsp --help` and `swift --version` successfully under
`--platform linux/arm64` after installing Swift's own documented runtime
dependencies) — so, like clangd, there is **no arm64-specific override
needed at all**. Unlike clangd, however, Debian's own package archive does
NOT carry a `sourcekit-lsp`/Swift package — Apple only ships toolchain
tarballs directly, not .debs — so this is installed via a pinned tarball
download in the Dockerfile (same pattern as Rust's rustup/Go's tarball),
not `apt-get install`.

Image size warning: the full Swift toolchain is ~3.3GB unpacked — by far
the largest of any language server shipped so far (bigger than the .NET
SDK's ~600MB or Rust's ~600MB combined). This is unavoidable: sourcekit-lsp
is not distributed standalone, only as part of the full toolchain (it
needs the Swift/Clang compiler libraries it links against at runtime).
Worth a specific call on whether Swift support justifies this image size
increase for the actual client project mix, since it dwarfs every other
per-language cost paid so far — see docs/lsp-integration.md for the tradeoff
write-up.
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
# doing a `Language(language)` enum lookup for it, same as CPP_LANGUAGE_ID.
SWIFT_LANGUAGE_ID = "swift"


class SwiftLanguageServer(LanguageServer):
    """
    Swift support via Apple's `sourcekit-lsp`, installed at build time as
    part of the official Swift toolchain tarball (see Dockerfile). Modeled
    directly on `clangd_language_server.py` / multilspy's own `Gopls`
    class — the binary is already on `PATH`, no per-request download or
    manifest handling needed.
    """

    @staticmethod
    def _get_sourcekit_lsp_version() -> str | None:
        try:
            result = subprocess.run(
                ["sourcekit-lsp", "--help"], capture_output=True, text=True
            )
            if result.returncode == 0:
                return "sourcekit-lsp available"
        except FileNotFoundError:
            return None
        return None

    @classmethod
    def setup_runtime_dependency(cls) -> bool:
        version = cls._get_sourcekit_lsp_version()
        if not version:
            raise RuntimeError(
                "Missing required dependency: sourcekit-lsp. Install the "
                "official Swift toolchain from "
                "https://www.swift.org/install/linux/ (sourcekit-lsp ships "
                "inside usr/bin of the toolchain tarball, not standalone)."
            )
        return True

    def __init__(self, config: MultilspyConfig, logger: MultilspyLogger, repository_root_path: str):
        self.setup_runtime_dependency()

        # SwiftPM-based projects need sourcekit-lsp to write build/index
        # artifacts (.build/index-build/...) — the ONLY language server
        # shipped so far with this requirement, since every other server
        # here only ever reads project source files. This bridge always
        # bind-mounts project roots read-only (see scripts/start.sh), so
        # --scratch-path is required to redirect that write traffic to a
        # writable location inside the bridge container's own filesystem
        # instead of the read-only host mount — confirmed via a live
        # failure without it ("I/O error (code: 30) while reading
        # .../.build/index-build/repositories").
        scratch_path = os.path.join(
            "/tmp/sourcekit-scratch", os.path.basename(repository_root_path.rstrip("/"))
        )
        os.makedirs(scratch_path, exist_ok=True)

        super().__init__(
            config,
            logger,
            repository_root_path,
            ProcessLaunchInfo(
                cmd=f"sourcekit-lsp --scratch-path {scratch_path}", cwd=repository_root_path
            ),
            SWIFT_LANGUAGE_ID,
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
    async def start_server(self) -> AsyncIterator["SwiftLanguageServer"]:
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
            self.logger.log("Starting sourcekit-lsp server process", logging.INFO)
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
