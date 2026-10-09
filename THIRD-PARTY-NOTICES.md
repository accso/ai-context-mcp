# Third-Party Notices

This project is licensed under the Apache License, Version 2.0 (see
`LICENSE`). The LSP-bridge Docker images bundle the following
third-party components, each under its own license. Only the
languages/servers actually built into an image are bundled; none of
these binaries are checked into this git repository.

See [`docs/lsp-integration.md`](.ai-context/docs/lsp-integration.md#licensing)
for the full per-dependency licensing rationale.

| Language | Server | License |
|---|---|---|
| TypeScript/JS | `typescript-language-server` (wraps `tsserver`) | MIT |
| Python | `jedi-language-server` | MIT |
| Rust | `rust-analyzer` | MIT / Apache-2.0 (dual) |
| C# | OmniSharp (`omnisharp-roslyn`) | MIT |
| Kotlin | `kotlin-language-server` (fwcd) | MIT |
| Ruby | Solargraph | MIT |
| C/C++ | `clangd` (LLVM) | Apache-2.0 WITH LLVM-exception |
| Swift | `sourcekit-lsp` (Apple) | Apache-2.0 |
| Go | `gopls` (`golang.org/x/tools`) | BSD-3-Clause |
| Dart | `dart-language-server` (bundled with Dart SDK) | BSD-3-Clause |
| — | [`multilspy`](https://github.com/microsoft/multilspy) 0.0.15 — Python library `lsp-bridge` imports directly to drive most servers above over LSP JSON-RPC | MIT |

## Not bundled by default: Java / Eclipse JDT Language Server (EPL-2.0)

Java support is not auto-enabled and never baked into any image we
publish — it downloads directly from Red Hat's servers only if a user
explicitly sets `lsp.accept_java_epl_license: true` in their own
`config.yaml` (see [Configuration](.ai-context/docs/configuration.md)
and [Licensing](.ai-context/docs/lsp-integration.md#licensing)).

## Notes

- MIT and BSD-3-Clause dependencies require no further action
  ([Apache's bundling guidance](https://infra.apache.org/licensing-howto.html)):
  permissive, license text travels with the binary/package.
- Apache-2.0 dependencies (`clangd`, `sourcekit-lsp`, `rust-analyzer`)
  ship no `NOTICE`/`COPYRIGHT` file of their own (verified 2026-09-08
  against each upstream repo) — nothing to bubble up into this
  project's `NOTICE`. Re-check if any of them ever adds one.

