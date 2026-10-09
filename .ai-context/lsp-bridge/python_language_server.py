"""
python_language_server.py — DependencyAwareJediServer subclass.

Fixes two things multilspy's bundled JediServer gets wrong for our use
case:

1. **Upstream bug**: multilspy's own
   `multilspy/language_servers/jedi_language_server/initialize_params.json`
   has an `initializationOptions` block that is actually a copy-paste of
   rust-analyzer's settings schema (verified: it contains `cargoRunner`,
   `cargo`, `rustfmt`, `procMacro`, etc. — nothing jedi-language-server
   understands). jedi-language-server's own settings parser
   (`jedi_language_server/initialization_options.py`, cattrs-based)
   silently ignores keys it doesn't recognize and falls back to defaults
   for everything else, so this template has never actually configured
   jedi-language-server at all — it's inert, not just wrong.

2. **Per-project dependency isolation** (see "Dependency resolution" in
   docs/lsp-integration.md, Python row):
   `dependency_restore.py` installs each project's resolved packages into
   a project_key-namespaced directory (`pip install --target ...`)
   instead of a flat shared site-packages, because pip installs aren't
   version-keyed on disk. Nothing on jedi's default search path points at
   that directory unless we tell it to via
   `initializationOptions.workspace.extraPaths` — verified against
   jedi_language_server/initialization_options.py's `Workspace` dataclass.

Without this override, resolve_symbol on a Python project either finds
nothing beyond stdlib/whatever happens to already be on the container's
own global site-packages (a false-positive risk when a package used by
this LSP bridge itself, e.g. `requests`, happens to share a name with a
target project's dependency), never the project's actually-restored
version.
"""
from __future__ import annotations

import os
import pathlib

from multilspy.language_servers.jedi_language_server.jedi_server import JediServer
from multilspy.lsp_protocol_handler.lsp_types import InitializeParams


class DependencyAwareJediServer(JediServer):
    def __init__(self, config, logger, repository_root_path: str, extra_paths: list[str] | None = None):
        super().__init__(config, logger, repository_root_path)
        self._extra_paths = extra_paths or []

    def _get_initialize_params(self, repository_absolute_path: str) -> InitializeParams:
        d = super()._get_initialize_params(repository_absolute_path)
        # Replace the entire block rather than patch it — the inherited
        # value is the inert rust-analyzer template described above, so
        # merging into it would just carry that garbage forward.
        d["initializationOptions"] = {
            "workspace": {
                "extraPaths": list(self._extra_paths),
            }
        }
        return d
