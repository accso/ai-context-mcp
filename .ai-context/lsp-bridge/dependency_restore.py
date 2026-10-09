"""
dependency_restore.py — Lazy, staleness-gated dependency restore step for
the LSP bridge, one per (project_key, language). See
"Dependency resolution" in docs/lsp-integration.md for the full design and
per-language root-cause table.

Called from LanguageServerPool._spawn (main.py), BEFORE the language
server itself starts — this deliberately reuses the same StillStarting /
per-language STARTUP_HINT_SECONDS retry mechanism already built for slow
LSP cold starts (see main.py's module docstring and
docs/lsp-integration.md), so a slow first-time restore
(e.g. `go mod download` on a large module graph) reports "still starting,
retry in Ns" to the agent instead of a generic timeout — no new signaling
mechanism needed.

Only ecosystems whose real dependency cache lives OUTSIDE root_path need a
restore step at all — TypeScript's node_modules is already inside
root_path by convention (accidental, but sufficient), so it's
intentionally absent from _RESTORE_SPECS below. C/C++ has no single
dependency-cache model this project can assume (see docs/lsp-integration.md),
so it's
absent too.

Read-only-mount caveat (see docs/lsp-integration.md): root_path is bind-mounted read-only
into this container, and must stay that way — it's the user's real repo
on disk, not a container-private copy. Every restore command below either
writes only to its own external cache volume (Go/Rust/Ruby), or is
redirected away from the project tree via a real CLI flag
(C#: -p:BaseIntermediateOutputPath/-p:MSBuildProjectExtensionsPath; Swift:
--scratch-path; Python: --target). Dart has no such flag for its required
`.dart_tool/package_config.json` write — that language is intentionally
left degraded (best-effort, logged, never raised) rather than building a
shadow-copy fallback in this round.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("lsp-bridge.restore")


@functools.lru_cache(maxsize=1)
def ruby_shared_gem_path() -> str | None:
    """Solargraph is launched by multilspy as a bare global executable
    (`solargraph stdio`, NOT `bundle exec solargraph stdio` — confirmed
    via live validation that `bundle exec` fails outright here, since
    solargraph itself is a globally gem-installed tool, not a dependency
    declared in any project's own Gemfile). Its own gem lookup
    (`Gem::Specification.find_by_name`, used internally when it detects
    a workspace Gemfile.lock) is governed purely by GEM_PATH at process
    start, not anything BUNDLE_* controls at runtime.

    BUNDLE_PATH is one shared cache volume across every ruby project
    (not project-namespaced like pip/NuGet — gem installs ARE
    version-keyed on disk via <BUNDLE_PATH>/<ruby engine>/<ABI version>,
    unlike pip), so the resulting GEM_PATH entry is identical for every
    project and only needs computing once, here, rather than per-project
    in restore_dependencies.

    Returns the FULL desired GEM_PATH (default system paths PREPENDED
    with our cache dir), not just our dir alone — confirmed via live
    validation that overriding GEM_PATH to our dir alone (dropping the
    defaults) breaks solargraph itself: it can no longer find its own
    dependencies (e.g. `racc`, installed system-wide alongside
    solargraph at build time, not in our cache). Returns None if
    BUNDLE_PATH isn't set (Ruby not enabled for this build), `ruby` isn't
    on PATH, or `gem environment gempath` fails."""
    bundle_path = os.environ.get("BUNDLE_PATH")
    if not bundle_path:
        return None
    try:
        ruby_result = subprocess.run(
            ["ruby", "-e", 'print RbConfig::CONFIG["ruby_version"]'],
            check=True, capture_output=True, text=True, timeout=10,
        )
        gempath_result = subprocess.run(
            ["gem", "environment", "gempath"],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    ruby_abi_version = ruby_result.stdout.strip()
    default_gempath = gempath_result.stdout.strip()
    if not ruby_abi_version or not default_gempath:
        return None
    our_dir = str(Path(bundle_path) / "ruby" / ruby_abi_version)
    return f"{our_dir}{os.pathsep}{default_gempath}"

# Root of the per-project-namespaced install target for ecosystems where a
# flat shared cache would let two projects' conflicting dependency
# versions collide (Python) or would mix one project's resolved build
# graph with another's (Swift) — see plan §2's Python row and this
# module's Swift handling below.
_PIP_PROJECT_ROOT = "/dep-cache/pip/projects"
_SWIFTPM_PROJECT_ROOT = "/dep-cache/swiftpm"
_CSHARP_INTERMEDIATE_ROOT = "/dep-cache/nuget/projects"
_BUNDLE_APP_CONFIG_ROOT = "/dep-cache/gems/app-config"

# Languages known to need a project-tree write that the read-only mount
# blocks, with no confirmed redirect flag (see plan §3.2) — restore is
# still attempted (a project with no lockfile drift may not hit the write
# at all), but a read-only-filesystem-shaped failure is logged as an
# expected degradation, not a surprising crash, and never raised.
_DEGRADED_LANGUAGES = {"dart"}


@dataclass(frozen=True)
class _RestoreSpec:
    lockfile: str  # relative to repo_root; if absent, nothing to restore for this project
    cache_root_env: str  # env var scripts/start.sh sets for this language's cache mount
    command: tuple[str, ...]
    # If set, this language's install target is namespaced per-project
    # (via the given CLI flag) rather than writing into a flat shared
    # cache — see _PIP_PROJECT_ROOT/_SWIFTPM_PROJECT_ROOT above.
    project_scratch_flag: str | None = None
    project_scratch_root: str | None = None
    extra_args_fn: "callable | None" = field(default=None, repr=False)


_RESTORE_SPECS: dict[str, _RestoreSpec] = {
    "go": _RestoreSpec(
        lockfile="go.sum",
        cache_root_env="GOMODCACHE",
        command=("go", "mod", "download"),
    ),
    "rust": _RestoreSpec(
        lockfile="Cargo.lock",
        cache_root_env="CARGO_HOME",
        command=("cargo", "fetch"),
    ),
    "csharp": _RestoreSpec(
        # Falls back to *.csproj presence if packages.lock.json isn't
        # committed — see restore_dependencies below.
        lockfile="packages.lock.json",
        cache_root_env="NUGET_PACKAGES",
        command=("dotnet", "restore"),
    ),
    "ruby": _RestoreSpec(
        lockfile="Gemfile.lock",
        cache_root_env="BUNDLE_PATH",
        # --frozen refuses to modify Gemfile.lock rather than writing to
        # it — required since root_path is read-only (plan §3.2); if the
        # lockfile is out of sync with the Gemfile this fails loudly
        # instead of silently trying (and failing) to write it.
        command=("bundle", "install", "--frozen"),
    ),
    "python": _RestoreSpec(
        lockfile="requirements.txt",
        cache_root_env="PIP_CACHE_DIR",
        command=("pip", "install", "--no-cache-dir", "-r", "requirements.txt"),
        project_scratch_flag="--target",
        project_scratch_root=_PIP_PROJECT_ROOT,
    ),
    "dart": _RestoreSpec(
        lockfile="pubspec.lock",
        cache_root_env="PUB_CACHE",
        command=("dart", "pub", "get"),
    ),
    "swift": _RestoreSpec(
        lockfile="Package.resolved",
        cache_root_env="_SWIFTPM_CACHE_ROOT",  # not a real tool env var — see _state_dir
        command=("swift", "package", "resolve"),
        project_scratch_flag="--scratch-path",
        project_scratch_root=_SWIFTPM_PROJECT_ROOT,
    ),
}


class RestoreError(Exception):
    """Raised when a dependency restore command actually ran and failed
    (never raised when there's simply nothing to restore, or for
    languages in _DEGRADED_LANGUAGES). Callers must log this clearly and
    let the language server start anyway — a failed restore degrades
    dependency-code navigation only; it must never block project-local
    code navigation."""

    def __init__(self, language: str, project_key: str, returncode: int, stderr_tail: str):
        super().__init__(
            f"Dependency restore failed for {language}/{project_key} (exit {returncode}): {stderr_tail}"
        )
        self.language = language
        self.project_key = project_key
        self.returncode = returncode
        self.stderr_tail = stderr_tail


def _state_dir(language: str, spec: _RestoreSpec) -> Path:
    """Where restore state (lockfile-hash cache) is persisted for this
    language — reuses the same mounted, persistent cache volume as the
    dependency cache itself, so state survives container restarts exactly
    like the cache it's tracking."""
    if language == "swift":
        # No single real env var for SPM's cache; the mount itself
        # (see scripts/start.sh) is at /dep-cache/swiftpm.
        cache_root = _SWIFTPM_PROJECT_ROOT
    else:
        cache_root = os.environ.get(spec.cache_root_env)
    if not cache_root:
        raise RuntimeError(
            f"{spec.cache_root_env} not set; {language} cache volume not mounted "
            f"(scripts/start.sh should have mounted this — check LSP_LANGUAGES)"
        )
    state_dir = Path(cache_root) / ".restore-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _java_kotlin_state_dir(language: str) -> Path:
    base = "/root/.m2" if language == "java" else "/root/.gradle"
    state_dir = Path(base) / ".ai-context-restore-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _csharp_intermediate_dir(project_key: str) -> str:
    """Per-project MSBuild intermediate-output redirect directory for C#
    (see module docstring / _CSHARP_INTERMEDIATE_ROOT). Computed via a
    shared helper so both `_run_restore` (which passes it to `dotnet
    restore` as -p: flags) and `restore_dependencies`'s return value
    (which main.py uses to set the SAME path as env vars for OmniSharp's
    own subprocess — see main.py's Language.CSHARP dispatch) always agree,
    even when restore is skipped (cache up to date / no lockfile yet)."""
    return str(Path(_CSHARP_INTERMEDIATE_ROOT) / project_key / "obj")


def _ruby_app_config_dir(project_key: str) -> str:
    """Per-project BUNDLE_APP_CONFIG redirect directory for Ruby (see
    module docstring / _BUNDLE_APP_CONFIG_ROOT). Shared helper so both
    `_run_restore` (bundle install) and `restore_dependencies`'s return
    value (used by main.py to set the SAME env var for solargraph's own
    subprocess — solargraph shells out to Bundler internally to resolve
    gem paths from the project's Gemfile.lock, and Bundler.settings reads
    .bundle/config from BUNDLE_APP_CONFIG if set, otherwise defaults to
    <project>/.bundle which is read-only) always agree, even when restore
    is skipped."""
    return str(Path(_BUNDLE_APP_CONFIG_ROOT) / project_key)


async def restore_dependencies(language: str, project_key: str, repo_root: str) -> str | None:
    """Idempotent, lockfile-hash-gated dependency restore for one
    (project_key, language) pair. No-op for languages with no restore
    spec (TypeScript, C/C++ — see module docstring). Called synchronously
    inside LanguageServerPool._spawn, before the language server itself
    starts, so its own StillStarting timeout budget covers this too.

    Returns a per-project directory the LSP server itself must be told
    about explicitly, because it's not on any path the server discovers
    unprompted:
      - python/swift: the per-project scratch/install directory (used for
        jedi's `initializationOptions.workspace.extraPaths` — see
        python_language_server.py).
      - csharp: the MSBuild intermediate-output redirect directory (used
        as BaseIntermediateOutputPath/MSBuildProjectExtensionsPath ENV
        VARS for OmniSharp's own subprocess — see main.py's
        Language.CSHARP dispatch. Our own `dotnet restore` subprocess
        redirects its OWN write via -p: flags, but that does NOT affect
        OmniSharp's separate, independent MSBuild project load, which
        needs the identical redirect or it tries to write bin/obj into
        the read-only project mount and fails — discovered via live
        validation, see "Dependency resolution" in docs/lsp-integration.md).
      - ruby: the BUNDLE_APP_CONFIG redirect directory (used as an ENV
        VAR for solargraph's own subprocess — see main.py's
        Language.RUBY dispatch. Same pattern as csharp: our own `bundle
        install` subprocess redirects its OWN .bundle/config write, but
        solargraph independently shells out to Bundler to resolve gem
        paths and needs the identical redirect or IT tries to write
        .bundle/config into the read-only project mount too — discovered
        via live validation).
    Returns None for every other language (go/rust: their restored
    dependencies already live somewhere the language server finds
    unprompted via GOMODCACHE/CARGO_HOME)."""
    if language in ("java", "kotlin"):
        await _restore_java_kotlin_sources(language, project_key, repo_root)
        return None

    spec = _RESTORE_SPECS.get(language)
    if spec is None:
        return None  # TypeScript / C++ / anything else: nothing to restore

    repo_root_path = Path(repo_root)
    lockfile_path = repo_root_path / spec.lockfile
    if language == "csharp":
        scratch_dir = _csharp_intermediate_dir(project_key)
    elif language == "ruby":
        scratch_dir = _ruby_app_config_dir(project_key)
    elif spec.project_scratch_flag and spec.project_scratch_root:
        scratch_dir = str(Path(spec.project_scratch_root) / project_key)
    else:
        scratch_dir = None
    if not lockfile_path.is_file():
        if language == "csharp" and not any(repo_root_path.glob("**/*.csproj")):
            return scratch_dir  # no packages.lock.json AND no *.csproj at all — nothing to restore
        elif language != "csharp":
            return scratch_dir  # no lockfile → nothing this project needs restored

    state_dir = _state_dir(language, spec)
    state_file = state_dir / f"{project_key}.sha256"
    current_hash = _hash_file(lockfile_path) if lockfile_path.is_file() else "no-lockfile-csproj-only"
    if state_file.is_file() and state_file.read_text().strip() == current_hash:
        log.info(f"[restore] {language}/{project_key}: cache up to date, skipping restore")
        return scratch_dir

    await _run_restore(language, project_key, repo_root, spec, state_file, current_hash)
    return scratch_dir


async def _run_restore(
    language: str,
    project_key: str,
    repo_root: str,
    spec: _RestoreSpec,
    state_file: Path,
    current_hash: str,
) -> None:
    env = dict(os.environ)
    command = list(spec.command)

    if spec.project_scratch_flag and spec.project_scratch_root:
        scratch_dir = Path(spec.project_scratch_root) / project_key
        scratch_dir.mkdir(parents=True, exist_ok=True)
        command += [spec.project_scratch_flag, str(scratch_dir)]

    if language == "csharp":
        # Redirect MSBuild's intermediate-output write away from the
        # read-only project tree (obj/) — see module docstring. Also see
        # main.py's Language.CSHARP dispatch: OmniSharp's OWN separate
        # MSBuild project load needs this same path set as env vars, not
        # just as -p: flags on this restore subprocess.
        intermediate_dir = Path(_csharp_intermediate_dir(project_key))
        intermediate_dir.mkdir(parents=True, exist_ok=True)
        command += [
            f"-p:BaseIntermediateOutputPath={intermediate_dir}/",
            f"-p:MSBuildProjectExtensionsPath={intermediate_dir}/",
        ]

    if language == "ruby":
        # `bundle install` always tries to write <project>/.bundle/config
        # (to remember --frozen/--path etc. across invocations) regardless
        # of --frozen — discovered via live validation: it fails with
        # Errno::EROFS against the read-only project mount. BUNDLE_APP_CONFIG
        # redirects where that config file itself lives, independent of
        # BUNDLE_PATH (which already redirects the installed *gems*).
        app_config_dir = Path(_ruby_app_config_dir(project_key))
        app_config_dir.mkdir(parents=True, exist_ok=True)
        env["BUNDLE_APP_CONFIG"] = str(app_config_dir)

    log.info(f"[restore] {language}/{project_key}: restoring (lockfile changed or first run)...")
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=repo_root,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        stderr_tail = stderr.decode(errors="replace")[-2000:]
        if language in _DEGRADED_LANGUAGES:
            log.warning(
                f"[restore] {language}/{project_key}: restore failed (expected — {language} needs "
                f"a project-tree write the read-only mount blocks with no confirmed redirect flag, "
                f"see docs/lsp-integration.md). Dependency-code navigation stays "
                f"degraded for this project; project-local code navigation is unaffected. "
                f"stderr: {stderr_tail}"
            )
            return
        log.warning(f"[restore] {language}/{project_key}: restore FAILED. stderr: {stderr_tail}")
        raise RestoreError(language, project_key, proc.returncode, stderr_tail)

    state_file.write_text(current_hash)
    log.info(f"[restore] {language}/{project_key}: restore complete, cache updated")


async def _restore_java_kotlin_sources(language: str, project_key: str, repo_root: str) -> None:
    """Java/Kotlin Phase 5 spike (plan §2, §4): try to auto-fetch real
    dependency SOURCE (not just compiled bytecode) via Maven's/Gradle's
    own sources-classifier resolution into the already-mounted ~/.m2 /
    ~/.gradle cache volumes. Unlike every language above, success here is
    CONDITIONAL on the dependency actually publishing a -sources.jar —
    many transitive/internal artifacts don't. Failure is expected/common,
    not a real problem: log at INFO, never raise, never treated as a hard
    restore failure."""
    repo_root_path = Path(repo_root)
    is_maven = (repo_root_path / "pom.xml").is_file()
    gradle_kts = repo_root_path / "build.gradle.kts"
    gradle_groovy = repo_root_path / "build.gradle"
    is_gradle = gradle_kts.is_file() or gradle_groovy.is_file()
    if not is_maven and not is_gradle:
        return

    state_dir = _java_kotlin_state_dir(language)
    lockfile = (repo_root_path / "pom.xml") if is_maven else (gradle_kts if gradle_kts.is_file() else gradle_groovy)
    state_file = state_dir / f"{project_key}.sha256"
    current_hash = _hash_file(lockfile)
    if state_file.is_file() and state_file.read_text().strip() == current_hash:
        log.info(f"[restore] {language}/{project_key}: cache up to date, skipping sources fetch")
        return

    if is_maven:
        command = (
            "mvn", "-q", "dependency:resolve",
            "-Dclassifier=sources",
            "-Dmdep.failOnMissingClassifierArtifact=false",
        )
    else:
        # Best-effort: not every build.gradle(.kts) defines a task that
        # explicitly fetches sources jars. This runs Gradle's own
        # dependency resolution, which populates ~/.gradle/caches with
        # whatever sources ARE resolvable — confirmed as an open
        # verification item in plan §2's Kotlin row, not a guaranteed win.
        command = ("gradle", "--no-daemon", "dependencies", "--refresh-dependencies")

    log.info(f"[restore] {language}/{project_key}: attempting dependency SOURCE fetch (best-effort)...")
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=repo_root,
        env=dict(os.environ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    # Record the hash regardless of outcome — no point re-attempting an
    # unresolvable sources fetch on every spawn if the manifest hasn't
    # changed; a genuinely new dependency version will re-trigger this.
    state_file.write_text(current_hash)

    if proc.returncode != 0:
        log.info(
            f"[restore] {language}/{project_key}: sources fetch did not fully succeed (exit "
            f"{proc.returncode}) — common when dependencies don't publish -sources.jar; "
            f"navigation for those dependencies stays excluded, project-local code is "
            f"unaffected. stderr tail: {stderr.decode(errors='replace')[-500:]}"
        )
        return

    log.info(f"[restore] {language}/{project_key}: dependency source fetch complete")
