FROM python:3.12-slim

# Which LSP language toolchains to actually install in THIS build, as a
# comma-delimited list (e.g. "python,typescript,rust") — passed via
# --build-arg by start.sh, computed as the union of every registered
# project's auto-detected lsp.enabled_languages in .resolved.json. Only the
# toolchains actually needed by currently-registered projects get installed;
# see docs/lsp-integration.md for the writeup on why
# installing all languages unconditionally made every cache-invalidating
# rebuild re-download toolchains (e.g. the ~3.3GB Swift toolchain) that no
# project even uses.
ARG LSP_LANGUAGES=""

WORKDIR /app

# ── Base tooling needed by nearly every per-language install step below ───────
# curl/ca-certificates are used by several unconditional-looking steps below
# (Go/Rust toolchain downloads, restore-related tools) that don't themselves
# live inside the typescript-conditional block. Previously curl was only
# installed as part of the typescript block, which meant any build selecting
# Go/Rust/etc. WITHOUT typescript failed outright (confirmed via a live build
# with LSP_LANGUAGES=go,rust: "curl: not found" on the Go toolchain step) —
# fixed here by installing it unconditionally, once, regardless of which
# languages are selected.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js runtime (for typescript-language-server) ──────────────────────────
# Pinned at build time (see docs/lsp-integration.md: "Language servers
# pinned at known-good versions ... not fetched ad hoc at
# runtime — reproducibility + supply-chain control"). Node 20 LTS matches the
# version validated during initial rollout.
RUN case ",$LSP_LANGUAGES," in \
      *,typescript,*) \
        apt-get update && apt-get install -y --no-install-recommends gnupg \
        && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
        && apt-get install -y --no-install-recommends nodejs \
        && rm -rf /var/lib/apt/lists/* \
        ;; \
      *) echo "[build] Skipping Node.js/typescript (not in LSP_LANGUAGES)" ;; \
    esac

# typescript-language-server + typescript, pinned to the versions verified in
# initial rollout (see docs/lsp-integration.md).
RUN case ",$LSP_LANGUAGES," in \
      *,typescript,*) npm install -g typescript-language-server@4.3.3 typescript@5.7.2 ;; \
      *) echo "[build] Skipping typescript-language-server (not in LSP_LANGUAGES)" ;; \
    esac

# ── Go toolchain + gopls (Go team's own first-party language server) ──────────
# gopls (`golang.org/x/tools/gopls`) is BSD-3-Clause, not MIT/Apache-2.0 —
# a deliberate, explicit exception to this project's own MIT/Apache-2.0
# constraint, approved by the project owner (see "Licensing" in
# docs/lsp-integration.md).
# BSD-3-Clause is a permissive license practically equivalent to MIT (no
# copyleft; the only textual addition is a non-endorsement clause) — the
# one real substantive gap vs. Apache-2.0 is the lack of an express patent
# grant, which MIT itself also lacks, so this isn't a materially different
# risk profile than the MIT dependencies already accepted elsewhere in this
# image. Both Go and gopls publish official linux-arm64 builds/releases —
# unlike Rust/C#/Kotlin, no bridge-side arm64 override is needed at all:
# multilspy's `Gopls` class (multilspy/language_servers/gopls/gopls.py)
# has no manifest/platform-assert logic whatsoever, it just shells out to
# `go`/`gopls` already on PATH.
RUN case ",$LSP_LANGUAGES," in \
      *,go,*) \
        curl -fsSL https://go.dev/dl/go1.26.5.linux-arm64.tar.gz -o /tmp/go.tar.gz \
        && tar -C /usr/local -xzf /tmp/go.tar.gz \
        && rm /tmp/go.tar.gz \
        ;; \
      *) echo "[build] Skipping Go toolchain (not in LSP_LANGUAGES)" ;; \
    esac
ENV PATH="/usr/local/go/bin:/root/go/bin:${PATH}"
RUN case ",$LSP_LANGUAGES," in \
      *,go,*) go install golang.org/x/tools/gopls@v0.23.0 ;; \
      *) echo "[build] Skipping gopls (not in LSP_LANGUAGES)" ;; \
    esac

# ── Ruby + Solargraph (no arm64 override needed — see arm64_lsp_overrides.py
# module docstring for why) ────────────────────────────────────────────────────
# solargraph is MIT-licensed. Pinned to the same version multilspy's own
# runtime_dependencies.json specifies, installed at build time (instead of
# multilspy's own lazy runtime install-if-missing check) for the same
# reproducibility reasons already applied to every other language here.
# ruby-dev + build-essential are required because solargraph's `prism`
# dependency ships a native C extension that needs to compile at install
# time (confirmed via a live build failure without them:
# "mkmf.rb can't find header files for ruby at .../ruby.h").
# Debian's ruby package only ships VERSIONED binstubs (bundle3.3,
# bundler3.3) — unlike `ruby` itself, there's no unversioned `bundle`
# symlink installed by default (confirmed live: `which bundle` fails even
# though `gem list bundler` shows it present as a default gem). Our own
# restore step (dependency_restore.py) invokes the plain `bundle` command
# name, so without this symlink `asyncio.create_subprocess_exec` fails
# with FileNotFoundError trying to exec a binary that doesn't exist under
# that name at all — this was never a PATH/env issue.
RUN case ",$LSP_LANGUAGES," in \
      *,ruby,*) \
        apt-get update && apt-get install -y --no-install-recommends ruby ruby-dev rubygems build-essential \
        && rm -rf /var/lib/apt/lists/* \
        && gem install solargraph -v 0.51.1 \
        && ln -sf "$(command -v bundle3.3 || command -v bundle)" /usr/local/bin/bundle \
        && ln -sf "$(command -v bundler3.3 || command -v bundler)" /usr/local/bin/bundler \
        ;; \
      *) echo "[build] Skipping Ruby/Solargraph (not in LSP_LANGUAGES)" ;; \
    esac

# ── Swift toolchain (for sourcekit-lsp, no multilspy dispatch at all —
# see swift_language_server.py module docstring) ───────────────────────────────
# sourcekit-lsp is Apple's own first-party server, plain Apache-2.0
# (confirmed via LICENSE.txt in apple/sourcekit-lsp — no LLVM-exception
# wrinkle, unlike clangd). Unlike every other language here, Apple only
# publishes a full toolchain tarball, not a standalone package/binary —
# sourcekit-lsp links against the Swift/Clang compiler libraries at
# runtime, so it cannot be split out. This is by far the largest
# per-language cost paid so far (~3.3GB unpacked, vs. ~600MB each for the
# Rust toolchain and .NET SDK) — see
# docs/lsp-integration.md for the size
# tradeoff write-up before enabling this for a real client project.
#
# The runtime dependency list, GPG signing key, and download/verify
# pattern below are copied directly from Swift's own official
# swiftlang/swift-docker Dockerfile for debian12 (the closest match to
# this image's Debian trixie base — confirmed compatible: sourcekit-lsp
# and swift --version both run cleanly under --platform linux/arm64 once
# these runtime deps are present) rather than reinvented, since Apple/the
# Swift project maintain that install recipe as the canonical reference.
# One change on top of the upstream recipe: `gnupg` (the full metapackage)
# instead of just `gpg` — Debian trixie's minimal `gpg` package pulls in
# neither `dirmngr` nor `gpg-agent`, and `gpg --keyserver ... --recv-keys`
# needs both (confirmed via two separate live build failures, one per
# missing tool: "gpg: can't connect to the dirmngr", then "gpg: can't
# connect to the gpg-agent", each only surfacing once the previous one
# was fixed). `gnupg` is purged again after verifying the signature (see
# `apt-get purge` below) since none of it is needed at runtime.
#
# IMPORTANT ordering note: this MUST run BEFORE the clangd apt-get install
# below, not after. The Swift toolchain tarball bundles its own `clangd`
# binary at usr/bin/clangd (a different build,
# swiftlang/llvm-project-based, not Debian's) and extracts straight over
# `/` — installing it after Debian's clangd package would silently
# overwrite the pinned/verified Debian clangd binary with an
# undocumented, unpinned version, breaking both the clangd rollout's own
# reproducibility guarantee and (since sourcekit-lsp itself shells out to
# whatever `clangd` is on PATH for embedded C-family code) potentially
# changing sourcekit-lsp's own behavior too. Confirmed via a live repro:
# swapping the order back once caused `clangd --version` to report LLVM's
# bundled 21.0.0 instead of Debian's pinned 19.1.7.
ARG SWIFT_SIGNING_KEY=52BB7E3DE28A71BE22EC05FFEF80A866B47A981F
ARG SWIFT_PLATFORM=debian12
ARG SWIFT_BRANCH=swift-6.3.3-release
ARG SWIFT_VERSION=swift-6.3.3-RELEASE
ARG SWIFT_WEBROOT=https://download.swift.org
RUN case ",$LSP_LANGUAGES," in \
      *,swift,*) \
        apt-get update && apt-get install -y --no-install-recommends \
            binutils libicu-dev libcurl4-openssl-dev libedit-dev libsqlite3-dev \
            libncurses-dev libpython3-dev libxml2-dev pkg-config uuid-dev \
            tzdata git gcc libstdc++-12-dev gnupg \
        && rm -rf /var/lib/apt/lists/* \
        && ARCH_NAME="$(dpkg --print-architecture)" \
        && case "${ARCH_NAME##*-}" in \
            'amd64') OS_ARCH_SUFFIX='' ;; \
            'arm64') OS_ARCH_SUFFIX='-aarch64' ;; \
            *) echo >&2 "error: unsupported architecture: '$ARCH_NAME'"; exit 1 ;; \
           esac \
        && SWIFT_WEBDIR="${SWIFT_WEBROOT}/${SWIFT_BRANCH}/$(echo $SWIFT_PLATFORM | tr -d .)${OS_ARCH_SUFFIX}" \
        && SWIFT_BIN_URL="${SWIFT_WEBDIR}/${SWIFT_VERSION}/${SWIFT_VERSION}-${SWIFT_PLATFORM}${OS_ARCH_SUFFIX}.tar.gz" \
        && SWIFT_SIG_URL="${SWIFT_BIN_URL}.sig" \
        && export GNUPGHOME="$(mktemp -d)" \
        && curl -fsSL "$SWIFT_BIN_URL" -o /tmp/swift.tar.gz "$SWIFT_SIG_URL" -o /tmp/swift.tar.gz.sig \
        && gpg --batch --quiet --keyserver keyserver.ubuntu.com --recv-keys "$SWIFT_SIGNING_KEY" \
        && gpg --batch --verify /tmp/swift.tar.gz.sig /tmp/swift.tar.gz \
        && tar -xzf /tmp/swift.tar.gz --directory / --strip-components=1 \
        && chmod -R o+r /usr/lib/swift \
        && rm -rf "$GNUPGHOME" /tmp/swift.tar.gz.sig /tmp/swift.tar.gz \
        && apt-get purge --auto-remove -y gnupg \
        && swift --version \
        ;; \
      *) echo "[build] Skipping Swift toolchain (not in LSP_LANGUAGES)" ;; \
    esac

# ── clangd (LLVM's C/C++ language server, no multilspy dispatch at all —
# see clangd_language_server.py module docstring) ──────────────────────────────
# clangd is "Apache-2.0 WITH LLVM-exception" (see
# "Licensing" in docs/lsp-integration.md — strictly more permissive
# than plain Apache-2.0, not an exception like Java/Go/Dart). Installed via
# Debian's own package (native arm64 build in the main archive, confirmed
# under --platform linux/arm64) rather than a pinned upstream download —
# the simplest rollout of any language shipped so far: no manifest, no
# per-architecture URL, no build step, and the same package/version on
# every architecture Docker builds this image for. MUST run after the
# Swift toolchain install above — see the ordering note there.
# Debian's clangd package is only actually installed when cpp is selected,
# but the ordering-sanity RUN still needs to run conditionally too — it only
# makes sense (and only has a clangd binary to check) when cpp was selected.
RUN case ",$LSP_LANGUAGES," in \
      *,cpp,*) \
        apt-get update && apt-get install -y --no-install-recommends clangd \
        && rm -rf /var/lib/apt/lists/* \
        && clangd --version | grep -q 'Debian clangd' \
        || (echo "ERROR: /usr/bin/clangd is not Debian's clangd — Swift toolchain install ordering regressed" && exit 1) \
        ;; \
      *) echo "[build] Skipping clangd (not in LSP_LANGUAGES)" ;; \
    esac

# ── Python dependencies ────────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# kotlin-language-server (fwcd, community, MIT — confirmed via GitHub's license
# API, not just the repo badge) + its own bundled JRE. multilspy self-installs
# both into its own package directory on first use (same self-vendoring
# pattern as typescript-language-server); triggering that download here at
# build time keeps it pinned/reproducible instead of adding first-request
# latency in production, matching the principle already applied to
# typescript-language-server above. See
# docs/lsp-integration.md for why this is
# fwcd's server and not JetBrains' official (Alpha) `kotlin-lsp`.
RUN case ",$LSP_LANGUAGES," in \
      *,kotlin,*) python3 -c "\
from multilspy.language_servers.kotlin_language_server.kotlin_language_server import KotlinLanguageServer; \
from multilspy.multilspy_config import Language, MultilspyConfig; \
from multilspy.multilspy_logger import MultilspyLogger; \
KotlinLanguageServer.setup_runtime_dependencies(None, MultilspyLogger(), MultilspyConfig.from_dict({'code_language': Language.KOTLIN}))" ;; \
      *) echo "[build] Skipping kotlin-language-server (not in LSP_LANGUAGES)" ;; \
    esac

# ── Maven + Gradle CLIs (dependency restore only, not the language servers
# themselves — JDTLS/kotlin-language-server bundle their own internal
# build-tool integration separately). Needed so
# docs/lsp-integration.md's dependency-resolution step can run
# `mvn dependency:resolve -Dclassifier=sources` / `gradle` against a
# project's real `pom.xml`/`build.gradle(.kts)` to populate the shared
# ~/.m2 / ~/.gradle cache volumes before resolve_symbol needs them.
# Debian's packaged versions are old but sufficient for dependency
# resolution (not for building/running a project) — same "simplest rollout"
# choice already made for clangd above.
RUN case ",$LSP_LANGUAGES," in \
      *,java,*|*,kotlin,*) \
        apt-get update && apt-get install -y --no-install-recommends maven gradle \
        && rm -rf /var/lib/apt/lists/* \
        ;; \
      *) echo "[build] Skipping Maven/Gradle CLIs (neither java nor kotlin in LSP_LANGUAGES)" ;; \
    esac

# ── Rust toolchain (for rust-analyzer's own workspace/metadata resolution) ─────
# rust-analyzer is a standalone binary, but it still shells out to `cargo`/
# `rustc` to resolve project structure (`cargo metadata`) — confirmed via a
# live spike where definition/references silently returned empty without a
# real toolchain present, even though rust-analyzer itself was running fine.
# Minimal profile only (rustc + cargo + rust-std, no docs/clippy) to bound
# image size; see docs/lsp-integration.md for
# the size tradeoff (~600MB) this adds.
RUN case ",$LSP_LANGUAGES," in \
      *,rust,*) curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal ;; \
      *) echo "[build] Skipping Rust toolchain (not in LSP_LANGUAGES)" ;; \
    esac
ENV PATH="/root/.cargo/bin:${PATH}"

# ── .NET SDK (for OmniSharp's MSBuild-based project loading) ──────────────────
# The .NET *runtime* alone is not enough — OmniSharp uses MSBuild (bundled
# with the SDK, not the runtime) to load .sln/.csproj files. Also installs
# libicu, without which OmniSharp's .NET host crashes immediately on startup
# with "Couldn't find a valid ICU package" (confirmed via a live spike).
# See docs/lsp-integration.md for the size
# tradeoff (~600MB) this adds, and for why linux-arm64 needs the
# `arm64_lsp_overrides.py` shim at all (multilspy's own OmniSharp support is
# hard-coded to linux-x64/win-x64 only, even though its own manifest already
# has working linux-arm64 URLs).
RUN case ",$LSP_LANGUAGES," in \
      *,csharp,*) \
        apt-get update && apt-get install -y --no-install-recommends libicu76 \
        && rm -rf /var/lib/apt/lists/* \
        && curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
        && bash /tmp/dotnet-install.sh --channel 8.0 --install-dir /usr/share/dotnet \
        && rm /tmp/dotnet-install.sh \
        && ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet \
        ;; \
      *) echo "[build] Skipping .NET SDK (not in LSP_LANGUAGES)" ;; \
    esac

# arm64_lsp_overrides.py: thin RustAnalyzer/OmniSharp/EclipseJDTLS/
# DartLanguageServer subclasses that fix narrow linux-arm64 gaps in
# multilspy 0.0.15 itself (missing rust-analyzer manifest row; hard-coded
# x64-only OmniSharp platform assert; missing derived vscode-java path
# fields; missing Dart SDK manifest row) despite multilspy's own manifest
# already having working arm64 download URLs for all four — see the
# module's own docstring and docs/lsp-integration.md
# status for the full rationale. On non-arm64 hosts these subclasses fall
# straight through to multilspy's unmodified behavior.
#
# Eclipse JDT Language Server (launched by Arm64EclipseJDTLS) is EPL-2.0
# licensed. We only ever download and run its official, unmodified upstream
# binary as a separate subprocess over stdio — same pattern as every other
# language server here, never modifying/forking/statically linking its
# code. See "Licensing — Java / EPL-2.0" in docs/lsp-integration.md for the full
# licensing analysis and the open legal/compliance action item.
COPY arm64_lsp_overrides.py .
RUN case ",$LSP_LANGUAGES," in \
      *,rust,*) python3 -c "\
from arm64_lsp_overrides import Arm64RustAnalyzer; \
from multilspy.multilspy_config import Language, MultilspyConfig; \
from multilspy.multilspy_logger import MultilspyLogger; \
Arm64RustAnalyzer.setup_runtime_dependencies(None, MultilspyLogger(), MultilspyConfig.from_dict({'code_language': Language.RUST}))" ;; \
      *) echo "[build] Skipping Arm64RustAnalyzer setup (rust not in LSP_LANGUAGES)" ;; \
    esac
RUN case ",$LSP_LANGUAGES," in \
      *,csharp,*) python3 -c "\
from arm64_lsp_overrides import Arm64OmniSharp; \
from multilspy.multilspy_config import Language, MultilspyConfig; \
from multilspy.multilspy_logger import MultilspyLogger; \
Arm64OmniSharp.setupRuntimeDependencies(None, MultilspyLogger(), MultilspyConfig.from_dict({'code_language': Language.CSHARP}))" ;; \
      *) echo "[build] Skipping Arm64OmniSharp setup (csharp not in LSP_LANGUAGES)" ;; \
    esac
RUN case ",$LSP_LANGUAGES," in \
      *,java,*) python3 -c "\
from arm64_lsp_overrides import Arm64EclipseJDTLS; \
from multilspy.multilspy_config import Language, MultilspyConfig; \
from multilspy.multilspy_logger import MultilspyLogger; \
Arm64EclipseJDTLS.setupRuntimeDependencies(None, MultilspyLogger(), MultilspyConfig.from_dict({'code_language': Language.JAVA}))" ;; \
      *) echo "[build] Skipping Arm64EclipseJDTLS setup (java not in LSP_LANGUAGES)" ;; \
    esac
RUN case ",$LSP_LANGUAGES," in \
      *,dart,*) python3 -c "\
from arm64_lsp_overrides import Arm64DartLanguageServer; \
from multilspy.multilspy_logger import MultilspyLogger; \
Arm64DartLanguageServer.setup_runtime_dependencies(None, MultilspyLogger())" ;; \
      *) echo "[build] Skipping Arm64DartLanguageServer setup (dart not in LSP_LANGUAGES)" ;; \
    esac

# Dart SDK's own `dart` CLI (not installed above — Arm64DartLanguageServer
# only sets up the language server binary itself). Needed so
# docs/lsp-integration.md's dependency-resolution step can
# attempt `dart pub get` — best-effort only (Dart is in _DEGRADED_LANGUAGES
# in dependency_restore.py: `.dart_tool/package_config.json` needs a
# project-tree write the read-only mount blocks, with no confirmed
# redirect flag yet).
ARG DART_SDK_VERSION=3.6.1
RUN case ",$LSP_LANGUAGES," in \
      *,dart,*) \
        ARCH_NAME="$(dpkg --print-architecture)" \
        && case "$ARCH_NAME" in amd64) DART_ARCH=x64 ;; arm64) DART_ARCH=arm64 ;; \
             *) echo >&2 "error: unsupported architecture for Dart SDK: $ARCH_NAME"; exit 1 ;; esac \
        && curl -fsSL "https://storage.googleapis.com/dart-archive/channels/stable/release/${DART_SDK_VERSION}/sdk/dartsdk-linux-${DART_ARCH}-release.zip" -o /tmp/dart.zip \
        && apt-get update && apt-get install -y --no-install-recommends unzip \
        && unzip -q /tmp/dart.zip -d /usr/local \
        && rm /tmp/dart.zip \
        && rm -rf /var/lib/apt/lists/* \
        ;; \
      *) echo "[build] Skipping Dart SDK (dart not in LSP_LANGUAGES)" ;; \
    esac
ENV PATH="/usr/local/dart-sdk/bin:${PATH}"

# clangd_language_server.py: from-scratch C/C++ subclass (no multilspy
# dispatch exists for this language at all — see its own module docstring).
# Build-time sanity check that clangd is actually on PATH, same "fail fast
# at build time, not first request" principle as every setup_runtime_
# dependencies() call above.
COPY clangd_language_server.py .
RUN case ",$LSP_LANGUAGES," in \
      *,cpp,*) python3 -c "\
from clangd_language_server import ClangdLanguageServer; \
ClangdLanguageServer.setup_runtime_dependency()" ;; \
      *) echo "[build] Skipping ClangdLanguageServer setup (cpp not in LSP_LANGUAGES)" ;; \
    esac

# swift_language_server.py: from-scratch Swift subclass (no multilspy
# dispatch exists for this language at all — see its own module
# docstring). Same build-time sanity check pattern as clangd above.
COPY swift_language_server.py .
RUN case ",$LSP_LANGUAGES," in \
      *,swift,*) python3 -c "\
from swift_language_server import SwiftLanguageServer; \
SwiftLanguageServer.setup_runtime_dependency()" ;; \
      *) echo "[build] Skipping SwiftLanguageServer setup (swift not in LSP_LANGUAGES)" ;; \
    esac

COPY main.py .
COPY dependency_restore.py .
COPY python_language_server.py .

# Bake the actually-installed language set into the runtime image as an ENV
# (ARG values only exist during the build stage) so main.py's startup log can
# report exactly what THIS build installed, not a hardcoded ceiling list —
# see docs/lsp-integration.md.
ENV LSP_LANGUAGES=${LSP_LANGUAGES}

# Pre-create the mount-point directory. Some container runtimes do not
# auto-create the parent directory for bind mounts, matching the same
# precaution taken in watcher/Dockerfile.
RUN mkdir -p /workspace

ENV RESOLVED_CONFIG_PATH=/app/.resolved.json
ENV WORKSPACE_ROOT=/workspace

EXPOSE 8090
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]
