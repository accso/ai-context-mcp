"""
arm64_lsp_overrides.py — linux-arm64 fixups for language servers multilspy
0.0.15 does not fully support out of the box on this architecture.

Why this file exists: this bridge only ever runs on developer Apple Silicon
Macs today (Docker Desktop's Linux VM there is linux/arm64) — see
docs/lsp-integration.md for the full
write-up. Both gaps below were confirmed live on an linux-arm64 container
to be narrow multilspy oversights, NOT missing upstream binaries:

- RustAnalyzer: multilspy's own runtime_dependencies.json (pinned to the
  2023-10-09 rust-analyzer release it uses for every other platform) is
  simply missing a linux-arm64 row. rust-analyzer itself DOES publish a
  linux-arm64 build for that exact release
  (rust-analyzer-aarch64-unknown-linux-gnu.gz) — verified the URL below
  resolves with HTTP 200.
- OmniSharp: multilspy's own runtime_dependencies.json ALREADY lists
  working linux-arm64 URLs for both OmniSharp and RazorOmnisharp, but
  setupRuntimeDependencies() hard-asserts the platform must be linux-x64
  or win-x64 — an overly narrow check in multilspy itself, not a real
  binary gap. Verified the linux-arm64 asset URL resolves with HTTP 200.
  Its linux-arm64 RazorOmnisharp manifest row is ALSO missing the
  "dll_path" key that every other platform's row has (same value on every
  platform) — a second, unrelated arm64 data gap in the same manifest,
  worked around below with a hardcoded fallback.
- EclipseJDTLS (Java): multilspy's own runtime_dependencies.json already
  lists a working linux-arm64 vscode-java download URL and the platform
  assert is already commented out upstream — but that linux-arm64 row is
  missing the five derived path fields present on every other platform's
  row (jre_home_path, jre_path, lombok_jar_path, jdtls_launcher_jar_path,
  jdtls_readonly_config_path). Confirmed by downloading and inspecting the
  actual v1.23.0 linux-arm64 vscode-java .vsix contents directly: the JRE
  ships under a "-linux-aarch64" (not "-linux-x86_64") directory name, and
  the bundled JDT LS config directory is "config_linux_arm" (not
  "config_linux") — everything else (lombok jar name, the platform-
  independent equinox launcher jar) is identical across linux-x64 and
  linux-arm64.

  NOTE ON LICENSING: Eclipse JDT Language Server (the actual LSP backend
  vscode-java bundles and this class launches) is EPL-2.0-licensed. This
  bridge only ever downloads and runs its official, unmodified upstream
  binary as a separate subprocess over stdio — exactly like every other
  language server here — and never modifies, forks, or statically links
  its code. See "Licensing — Java / EPL-2.0" in docs/lsp-integration.md for
  the full
  licensing analysis and the open legal/compliance action item; this is
  an engineering note, not a legal conclusion.
- DartLanguageServer: multilspy's own runtime_dependencies.json is pinned
  to the Dart SDK 3.7.1 stable release for linux-x64 only — same shape of
  gap as RustAnalyzer above, simply missing a linux-arm64 row. The Dart
  SDK itself DOES publish an official linux-arm64 archive for that exact
  release (dartsdk-linux-arm64-release.zip) — verified the URL resolves
  with HTTP 200. The internal zip layout (dart-sdk/bin/dart) is identical
  across architectures, so only the download URL/platformId needs adding.

(Ruby's `Solargraph` needed no override at all — it has no
platform-specific manifest logic; it just checks for `ruby`/`gem` on
`PATH` and installs the pinned `solargraph` gem if missing, which works
identically on any architecture.)

Both overrides are additive (reuse multilspy's own manifest data/logic,
just widen what's allowed) and only special-case linux-arm64 — every other
platform falls straight through to multilspy's own unmodified
implementation via `super()`. Delete this file (and the dispatch in
main.py that references it) once multilspy upstream adds real arm64
support for these two servers — check
https://github.com/microsoft/multilspy for progress before assuming this
is still needed.
"""
from __future__ import annotations

import json
import os
import stat

import multilspy.language_servers.dart_language_server.dart_language_server as _dart_mod
import multilspy.language_servers.eclipse_jdtls.eclipse_jdtls as _eclipse_jdtls_mod
import multilspy.language_servers.omnisharp.omnisharp as _omnisharp_mod
import multilspy.language_servers.rust_analyzer.rust_analyzer as _rust_analyzer_mod
from multilspy.language_servers.dart_language_server.dart_language_server import DartLanguageServer
from multilspy.language_servers.eclipse_jdtls.eclipse_jdtls import EclipseJDTLS, RuntimeDependencyPaths
from multilspy.language_servers.omnisharp.omnisharp import OmniSharp
from multilspy.language_servers.rust_analyzer.rust_analyzer import RustAnalyzer
from multilspy.multilspy_config import MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger
from multilspy.multilspy_utils import DotnetVersion, FileUtils, PlatformId, PlatformUtils

# Same rust-analyzer release multilspy itself pins for every other platform
# (see runtime_dependencies.json next to multilspy's own rust_analyzer.py) —
# kept in lockstep so every platform runs the identical rust-analyzer
# version, not just "whatever is latest today".
_RUST_ANALYZER_RELEASE_TAG = "2023-10-09"
_RUST_ANALYZER_ARM64_URL = (
    "https://github.com/rust-lang/rust-analyzer/releases/download/"
    f"{_RUST_ANALYZER_RELEASE_TAG}/rust-analyzer-aarch64-unknown-linux-gnu.gz"
)

# Same Dart SDK release multilspy itself pins for linux-x64 (see
# runtime_dependencies.json next to multilspy's own dart_language_server.py)
# — kept in lockstep so every platform runs the identical Dart SDK version.
_DART_SDK_RELEASE = "3.7.1"
_DART_SDK_ARM64_URL = (
    "https://storage.googleapis.com/dart-archive/channels/stable/release/"
    f"{_DART_SDK_RELEASE}/sdk/dartsdk-linux-arm64-release.zip"
)
_DART_SDK_BINARY_NAME = "dart-sdk/bin/dart"  # identical layout across architectures



# Same value on every platform in multilspy's own manifest — only the
# linux-arm64 row happens to omit this key (verified against every other
# platform's RazorOmnisharp entry in the same runtime_dependencies.json).
_RAZOR_OMNISHARP_DLL_PATH = "OmniSharpPlugin/Microsoft.AspNetCore.Razor.OmniSharpPlugin.dll"


# Derived path fragments confirmed by downloading and inspecting the real
# v1.23.0 linux-arm64 vscode-java .vsix contents directly (see module
# docstring). These are the arm64 equivalents of the fields multilspy's own
# manifest already provides for every other platform's row.
_JDTLS_ARM64_JRE_DIR_NAME = "17.0.8.1-linux-aarch64"
_JDTLS_ARM64_LOMBOK_JAR_NAME = "lombok-1.18.30.jar"
_JDTLS_ARM64_LAUNCHER_JAR_NAME = "org.eclipse.equinox.launcher_1.6.500.v20230717-2134.jar"
_JDTLS_ARM64_CONFIG_DIR_NAME = "config_linux_arm"


class Arm64RustAnalyzer(RustAnalyzer):
    """RustAnalyzer with a linux-arm64 runtime dependency added."""

    def setup_runtime_dependencies(self, logger: MultilspyLogger, config: MultilspyConfig) -> str:
        if PlatformUtils.get_platform_id() != PlatformId.LINUX_arm64:
            return super().setup_runtime_dependencies(logger, config)

        rustanalyzer_ls_dir = os.path.join(os.path.dirname(_rust_analyzer_mod.__file__), "static", "RustAnalyzer")
        rustanalyzer_executable_path = os.path.join(rustanalyzer_ls_dir, "rust_analyzer")
        if not os.path.exists(rustanalyzer_ls_dir):
            os.makedirs(rustanalyzer_ls_dir)
            FileUtils.download_and_extract_archive(
                logger, _RUST_ANALYZER_ARM64_URL, rustanalyzer_executable_path, "gz"
            )
        assert os.path.exists(rustanalyzer_executable_path)
        os.chmod(rustanalyzer_executable_path, stat.S_IEXEC)
        return rustanalyzer_executable_path


class Arm64OmniSharp(OmniSharp):
    """OmniSharp with the linux-arm64 platform assert widened.

    Mirrors multilspy's own OmniSharp.setupRuntimeDependencies() (see
    multilspy/language_servers/omnisharp/omnisharp.py) almost line for
    line — the only change is allowing PlatformId.LINUX_arm64 through,
    since multilspy's own runtime_dependencies.json already has working
    entries for it. Keep this in sync if multilspy changes that method.
    """

    def setupRuntimeDependencies(self, logger: MultilspyLogger, config: MultilspyConfig) -> tuple[str, str]:
        if PlatformUtils.get_platform_id() != PlatformId.LINUX_arm64:
            return super().setupRuntimeDependencies(logger, config)

        platform_id = PlatformId.LINUX_arm64
        dotnet_version = PlatformUtils.get_dotnet_version()

        with open(os.path.join(os.path.dirname(_omnisharp_mod.__file__), "runtime_dependencies.json")) as f:
            d = json.load(f)
            del d["_description"]

        assert dotnet_version in [
            DotnetVersion.V6,
            DotnetVersion.V7,
            DotnetVersion.V8,
        ], "Only dotnet version 6, 7 and 8 are supported for linux-arm64 OmniSharp"

        # Same fallback multilspy's own implementation uses: runtime binaries
        # aren't published separately for .Net 7/8, so .Net 6 binaries are
        # assumed compatible.
        if dotnet_version in [DotnetVersion.V7, DotnetVersion.V8]:
            dotnet_version = DotnetVersion.V6

        runtime_dependencies = d["runtimeDependencies"]
        runtime_dependencies = [
            dependency for dependency in runtime_dependencies if dependency["platformId"] == platform_id.value
        ]
        runtime_dependencies = [
            dependency
            for dependency in runtime_dependencies
            if not ("dotnet_version" in dependency) or dependency["dotnet_version"] == dotnet_version.value
        ]
        assert len(runtime_dependencies) == 2
        runtime_dependencies = {
            runtime_dependencies[0]["id"]: runtime_dependencies[0],
            runtime_dependencies[1]["id"]: runtime_dependencies[1],
        }

        assert "OmniSharp" in runtime_dependencies
        assert "RazorOmnisharp" in runtime_dependencies

        omnisharp_ls_dir = os.path.join(os.path.dirname(_omnisharp_mod.__file__), "static", "OmniSharp")
        if not os.path.exists(omnisharp_ls_dir):
            os.makedirs(omnisharp_ls_dir)
            FileUtils.download_and_extract_archive(
                logger, runtime_dependencies["OmniSharp"]["url"], omnisharp_ls_dir, "zip"
            )
        # multilspy's own manifest omits "binaryName" for this exact platform
        # row (present for every other platform's net6.0 entry) because this
        # build is framework-dependent, not a native binary — it must be
        # launched via `dotnet OmniSharp.dll`, not executed directly. Detect
        # that case rather than hardcoding it, so this still works if
        # multilspy's manifest ever gains a native arm64 binaryName later.
        binary_name = runtime_dependencies["OmniSharp"].get("binaryName")
        if binary_name is not None:
            omnisharp_executable_path = os.path.join(omnisharp_ls_dir, binary_name)
            assert os.path.exists(omnisharp_executable_path)
            os.chmod(omnisharp_executable_path, stat.S_IEXEC)
        else:
            omnisharp_dll_path = os.path.join(omnisharp_ls_dir, "OmniSharp.dll")
            assert os.path.exists(omnisharp_dll_path)
            omnisharp_executable_path = f"dotnet {omnisharp_dll_path}"

        razor_omnisharp_ls_dir = os.path.join(os.path.dirname(_omnisharp_mod.__file__), "static", "RazorOmnisharp")
        if not os.path.exists(razor_omnisharp_ls_dir):
            os.makedirs(razor_omnisharp_ls_dir)
            FileUtils.download_and_extract_archive(
                logger, runtime_dependencies["RazorOmnisharp"]["url"], razor_omnisharp_ls_dir, "zip"
            )
        razor_omnisharp_dll_path = os.path.join(
            razor_omnisharp_ls_dir, runtime_dependencies["RazorOmnisharp"].get("dll_path", _RAZOR_OMNISHARP_DLL_PATH)
        )
        assert os.path.exists(razor_omnisharp_dll_path)

        return omnisharp_executable_path, razor_omnisharp_dll_path


class Arm64EclipseJDTLS(EclipseJDTLS):
    """EclipseJDTLS (Java) with the linux-arm64 vscode-java derived paths filled in.

    NOTE ON LICENSING: this class only downloads and launches the official,
    unmodified upstream Eclipse JDT Language Server binary (EPL-2.0) as a
    separate subprocess over stdio — same pattern as every other language
    server in this bridge. Nothing here modifies, forks, or statically
    links EPL-2.0-covered code. See
    "Licensing — Java / EPL-2.0" in docs/lsp-integration.md for the full licensing
    analysis and the open legal/compliance action item before relying on
    that reading for a real deployment decision.

    Mirrors multilspy's own EclipseJDTLS.setupRuntimeDependencies() (see
    multilspy/language_servers/eclipse_jdtls/eclipse_jdtls.py) almost line
    for line — the only change is filling in the five path fields
    multilspy's own manifest omits for the linux-arm64 row. Keep this in
    sync if multilspy changes that method.
    """

    def setupRuntimeDependencies(self, logger: MultilspyLogger, config: MultilspyConfig) -> RuntimeDependencyPaths:
        if PlatformUtils.get_platform_id() != PlatformId.LINUX_arm64:
            return super().setupRuntimeDependencies(logger, config)

        with open(os.path.join(os.path.dirname(_eclipse_jdtls_mod.__file__), "runtime_dependencies.json")) as f:
            runtime_dependencies = json.load(f)
            del runtime_dependencies["_description"]

        os.makedirs(
            os.path.join(os.path.abspath(os.path.dirname(_eclipse_jdtls_mod.__file__)), "static"), exist_ok=True
        )

        gradle_path = os.path.join(
            os.path.abspath(os.path.dirname(_eclipse_jdtls_mod.__file__)), "static", "gradle-7.3.3"
        )
        if not os.path.exists(gradle_path):
            FileUtils.download_and_extract_archive(
                logger,
                runtime_dependencies["gradle"]["platform-agnostic"]["url"],
                os.path.dirname(gradle_path),
                runtime_dependencies["gradle"]["platform-agnostic"]["archiveType"],
            )
        assert os.path.exists(gradle_path)

        dependency = runtime_dependencies["vscode-java"][PlatformId.LINUX_arm64.value]
        vscode_java_path = os.path.join(
            os.path.abspath(os.path.dirname(_eclipse_jdtls_mod.__file__)),
            "static",
            dependency["relative_extraction_path"],
        )
        os.makedirs(vscode_java_path, exist_ok=True)
        jre_home_path = os.path.join(vscode_java_path, "extension", "jre", _JDTLS_ARM64_JRE_DIR_NAME)
        jre_path = os.path.join(jre_home_path, "bin", "java")
        lombok_jar_path = os.path.join(vscode_java_path, "extension", "lombok", _JDTLS_ARM64_LOMBOK_JAR_NAME)
        jdtls_launcher_jar_path = os.path.join(
            vscode_java_path, "extension", "server", "plugins", _JDTLS_ARM64_LAUNCHER_JAR_NAME
        )
        jdtls_readonly_config_path = os.path.join(
            vscode_java_path, "extension", "server", _JDTLS_ARM64_CONFIG_DIR_NAME
        )
        if not all(
            [
                os.path.exists(vscode_java_path),
                os.path.exists(jre_home_path),
                os.path.exists(jre_path),
                os.path.exists(lombok_jar_path),
                os.path.exists(jdtls_launcher_jar_path),
                os.path.exists(jdtls_readonly_config_path),
            ]
        ):
            FileUtils.download_and_extract_archive(
                logger, dependency["url"], vscode_java_path, dependency["archiveType"]
            )

        os.chmod(jre_path, stat.S_IEXEC)

        assert os.path.exists(vscode_java_path)
        assert os.path.exists(jre_home_path)
        assert os.path.exists(jre_path)
        assert os.path.exists(lombok_jar_path)
        assert os.path.exists(jdtls_launcher_jar_path)
        assert os.path.exists(jdtls_readonly_config_path)

        dependency = runtime_dependencies["intellicode"]["platform-agnostic"]
        intellicode_directory_path = os.path.join(
            os.path.abspath(os.path.dirname(_eclipse_jdtls_mod.__file__)),
            "static",
            dependency["relative_extraction_path"],
        )
        os.makedirs(intellicode_directory_path, exist_ok=True)
        intellicode_jar_path = os.path.join(intellicode_directory_path, dependency["intellicode_jar_path"])
        intellisense_members_path = os.path.join(intellicode_directory_path, dependency["intellisense_members_path"])
        if not all(
            [
                os.path.exists(intellicode_directory_path),
                os.path.exists(intellicode_jar_path),
                os.path.exists(intellisense_members_path),
            ]
        ):
            FileUtils.download_and_extract_archive(
                logger, dependency["url"], intellicode_directory_path, dependency["archiveType"]
            )

        assert os.path.exists(intellicode_directory_path)
        assert os.path.exists(intellicode_jar_path)
        assert os.path.exists(intellisense_members_path)

        return RuntimeDependencyPaths(
            gradle_path=gradle_path,
            lombok_jar_path=lombok_jar_path,
            jre_path=jre_path,
            jre_home_path=jre_home_path,
            jdtls_launcher_jar_path=jdtls_launcher_jar_path,
            jdtls_readonly_config_path=jdtls_readonly_config_path,
            intellicode_jar_path=intellicode_jar_path,
            intellisense_members_path=intellisense_members_path,
        )


class Arm64DartLanguageServer(DartLanguageServer):
    """DartLanguageServer with a linux-arm64 runtime dependency added.

    Mirrors Arm64RustAnalyzer's shape exactly: multilspy's own manifest is
    simply missing a linux-arm64 row (pinned to Dart SDK 3.7.1 for
    linux-x64 only), even though the Dart SDK itself DOES publish an
    official linux-arm64 archive for that same release.
    """

    def setup_runtime_dependencies(self, logger: MultilspyLogger) -> str:
        if PlatformUtils.get_platform_id() != PlatformId.LINUX_arm64:
            return super().setup_runtime_dependencies(logger)

        dart_ls_dir = os.path.join(os.path.dirname(_dart_mod.__file__), "static", "dart-language-server")
        dart_executable_path = os.path.join(dart_ls_dir, _DART_SDK_BINARY_NAME)
        if not os.path.exists(dart_ls_dir):
            os.makedirs(dart_ls_dir)
            FileUtils.download_and_extract_archive(logger, _DART_SDK_ARM64_URL, dart_ls_dir, "zip")
        assert os.path.exists(dart_executable_path)
        os.chmod(dart_executable_path, stat.S_IEXEC)

        return f"{dart_executable_path} language-server --client-id multilspy.dart --client-version 1.2"
