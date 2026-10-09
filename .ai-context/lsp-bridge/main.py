"""
main.py — LSP Bridge service.

One instance PER REPO (not per host, unlike the inference service — see
"Why per-repo" in docs/lsp-integration.md: this service needs
real project files bind-mounted, the inference service does not).

Owns a POOL of language-server CHILD PROCESSES, one live process per
(project_key, language) pair — never shared across projects (see
docs/lsp-integration.md). Each
process is spawned lazily on first request touching that project/language,
and evicted after an idle timeout to bound memory.

Endpoints:
  POST /resolve  { project_key, language, file_path, line, character, kind }
                 kind: "definition" | "references" | "hover"
                 -> normalized { results: [...] } (file_path/line convention
                    matches get_file/repo_structure: relative_path, 1-based
                    lines)
  GET  /health   { status, active_servers: [{project_key, language,
                    idle_seconds}] }

Only caller: project_server.ts (Phase 2). The agent never talks to this
service directly — it only ever sees the single `resolve_symbol` MCP tool.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from collections import defaultdict

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from multilspy import LanguageServer
from multilspy.multilspy_config import Language, MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger

from arm64_lsp_overrides import Arm64DartLanguageServer, Arm64EclipseJDTLS, Arm64OmniSharp, Arm64RustAnalyzer
from clangd_language_server import CPP_LANGUAGE_ID, ClangdLanguageServer
from dependency_restore import RestoreError, restore_dependencies, ruby_shared_gem_path
from python_language_server import DependencyAwareJediServer
from swift_language_server import SWIFT_LANGUAGE_ID, SwiftLanguageServer

log = logging.getLogger("lsp-bridge")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

RESOLVED_CONFIG_PATH = os.getenv("RESOLVED_CONFIG_PATH", "/app/.resolved.json")
WORKSPACE_ROOT = os.getenv("WORKSPACE_ROOT", "/workspace")
DEFAULT_IDLE_TIMEOUT_MINUTES = 15
IDLE_SWEEP_INTERVAL_SECONDS = 60

_KIND_HANDLERS = {"definition", "references", "hover"}

# The set of language servers actually baked into THIS running image, read
# from the LSP_LANGUAGES env var baked in at build time (see the ARG/ENV of
# the same name in the Dockerfile) — computed by start.sh as the union of
# every registered project's auto-detected lsp.enabled_languages, so only
# the toolchains actually needed get installed and this log line reflects
# reality instead of a hardcoded ceiling list.
#
# "python" is always included regardless of LSP_LANGUAGES: its LSP support
# (jedi-language-server) is an unconditional pip dependency in
# requirements.txt, not one of the Dockerfile's gated per-language install
# blocks, so it's always present in every build.
_ALWAYS_INSTALLED_LANGUAGES: tuple[str, ...] = ("python",)
INSTALLED_LANGUAGES: tuple[str, ...] = tuple(
    sorted(
        set(_ALWAYS_INSTALLED_LANGUAGES)
        | {lang.strip() for lang in os.getenv("LSP_LANGUAGES", "").split(",") if lang.strip()}
    )
)

# How long a single /resolve request will actually block waiting for a
# just-triggered cold start before giving up and telling the caller
# "still starting" instead of blocking until ITS OWN timeout fires (the
# real bug this fixes: previously the whole handshake ran under the
# request's coroutine with no bound of our own, so slow-starting servers
# just surfaced as an opaque axios timeout on the agent side).
SPAWN_WAIT_BUDGET_SECONDS = 8.0

# Conservative "retry after" hints for the caller, derived from real
# cold-start timings observed while shipping each language this project.
# Purely advisory — the actual spawn keeps running in the background
# regardless of how long any particular caller waited or how many gave up.
#
# Bumped for languages that now also run a dependency restore step first
# (see dependency_restore.py / docs/lsp-integration.md
# "Dependency resolution") — restore only actually runs on first use or lockfile drift
# (cached otherwise), but the FIRST spawn for a project can take
# meaningfully longer than the language server's own cold start alone.
STARTUP_HINT_SECONDS = {
    "java": 40.0,     # Eclipse JDT LS: workspace import + Maven/Gradle resolution + sources fetch spike
    "csharp": 35.0,   # OmniSharp project restore/resolution + our own `dotnet restore` for the NuGet cache
    "swift": 55.0,    # sourcekit-lsp indexing + `swift package resolve` into the SPM cache volume
    "kotlin": 30.0,   # kotlin-language-server + Gradle sources fetch spike
    "rust": 20.0,     # rust-analyzer crate graph load + `cargo fetch` into the shared registry cache
    "dart": 15.0,     # dart-language-server + best-effort `dart pub get` (may fail — see _DEGRADED_LANGUAGES)
    "go": 20.0,       # gopls + `go mod download` into the shared module cache
    "ruby": 15.0,     # solargraph + `bundle install --frozen` into the shared gem cache
    "python": 15.0,   # jedi-language-server + `pip install --target` into a per-project cache namespace
}
DEFAULT_STARTUP_HINT_SECONDS = 8.0


class StillStarting(Exception):
    """Raised when a language server's cold start hasn't finished within
    SPAWN_WAIT_BUDGET_SECONDS. Not an error — the spawn keeps running in
    the background; the caller is expected to retry."""

    def __init__(self, language: str, retry_after_seconds: float):
        super().__init__(f"Language server for '{language}' is still starting")
        self.language = language
        self.retry_after_seconds = retry_after_seconds


def _load_resolved_config() -> dict:
    with open(RESOLVED_CONFIG_PATH) as f:
        return json.load(f)


def _project_config(project_key: str) -> dict:
    resolved = _load_resolved_config()
    project = resolved.get("projects", {}).get(project_key)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Unknown project_key: {project_key}")
    return project


class _PooledServer:
    """One live language-server child process for one (project_key, language) pair."""

    def __init__(self, server: LanguageServer, context_manager, idle_timeout_minutes: float):
        self.server = server
        self._context_manager = context_manager
        self.idle_timeout_minutes = idle_timeout_minutes
        self.last_used = time.monotonic()

    def touch(self) -> None:
        self.last_used = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used

    def is_expired(self) -> bool:
        return self.idle_seconds > (self.idle_timeout_minutes * 60)

    async def stop(self) -> None:
        await self._context_manager.__aexit__(None, None, None)


class LanguageServerPool:
    """
    Lazily spawns and idle-evicts one language-server child process per
    (project_key, language) pair. See module docstring / plan §3.1 for why
    this is per-project, unlike the bridge container itself.
    """

    def __init__(self) -> None:
        self._servers: dict[tuple[str, str], _PooledServer] = {}
        # Spawns already in flight, kept keyed by (project_key, language) so
        # a caller that gives up waiting (StillStarting) doesn't trigger a
        # second, redundant spawn — the next request for the same key just
        # attaches to the same in-progress task.
        self._spawning: dict[tuple[str, str], asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def get_or_start(self, project_key: str, language: str) -> LanguageServer:
        key = (project_key, language)
        async with self._lock:
            pooled = self._servers.get(key)
            if pooled is not None:
                pooled.touch()
                return pooled.server

            task = self._spawning.get(key)
            if task is None:
                # Validate synchronously (raises HTTPException immediately
                # for bad requests — config lookup, unknown language,
                # missing mount — none of that should ever be deferred
                # behind a StillStarting retry).
                ml_language, repo_root, idle_timeout_minutes = self._validate(project_key, language)
                task = asyncio.create_task(
                    self._spawn(key, ml_language, repo_root, idle_timeout_minutes)
                )
                self._spawning[key] = task

        # Wait for the spawn OUTSIDE the lock so concurrent requests for
        # OTHER (project_key, language) pairs aren't blocked by this one's
        # cold start, and so we can bound our own wait without holding the
        # lock. If the budget is exceeded, the task keeps running in the
        # background — see StillStarting docstring above — and the next
        # request for this key will attach to it (or find it already done).
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=SPAWN_WAIT_BUDGET_SECONDS)
        except asyncio.TimeoutError:
            retry_after = STARTUP_HINT_SECONDS.get(language, DEFAULT_STARTUP_HINT_SECONDS)
            # Visible in container logs, not just in the response to the
            # caller: an operator tailing logs should see the same "still
            # starting" state the agent gets, not just silence until the
            # eventual "ready" line — otherwise a slow cold start looks
            # indistinguishable from a hang when watching the container.
            log.info(
                f"[pool] Still starting (caller gave up after {SPAWN_WAIT_BUDGET_SECONDS}s): "
                f"project={project_key} language={language}; retry_after={retry_after}s"
            )
            raise StillStarting(language, retry_after) from None

    def _validate(self, project_key: str, language: str) -> tuple[object, str, float]:
        project = _project_config(project_key)
        enabled_languages = set(project.get("lsp", {}).get("enabled_languages") or [])
        if language not in enabled_languages:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Language '{language}' is not enabled for project "
                    f"'{project_key}' (lsp.enabled_languages in config.yaml)."
                ),
            )
        try:
            # "cpp"/"swift" are not multilspy `Language` enum members —
            # multilspy has no native C/C++ or Swift support at all (see
            # clangd_language_server.py / swift_language_server.py module
            # docstrings). Handled as raw string code_languages instead of
            # going through multilspy's own Language(...) lookup, which
            # would raise ValueError for either.
            _CUSTOM_LANGUAGE_IDS = (CPP_LANGUAGE_ID, SWIFT_LANGUAGE_ID)
            ml_language = language if language in _CUSTOM_LANGUAGE_IDS else Language(language)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Unsupported language: {language}"
            ) from exc

        repo_root = str(Path(WORKSPACE_ROOT) / project_key)
        if not Path(repo_root).is_dir():
            raise HTTPException(
                status_code=404,
                detail=f"Project root not mounted inside bridge container: {repo_root}",
            )

        idle_timeout_minutes = float(
            project.get("lsp", {}).get("idle_timeout_minutes", DEFAULT_IDLE_TIMEOUT_MINUTES)
        )
        return ml_language, repo_root, idle_timeout_minutes

    async def _spawn(
        self, key: tuple[str, str], ml_language: object, repo_root: str, idle_timeout_minutes: float
    ) -> LanguageServer:
        """Actually spawn+initialize the language server child process for
        `key`. Runs as a background task so a slow cold start never blocks
        any single request coroutine beyond SPAWN_WAIT_BUDGET_SECONDS (see
        get_or_start / StillStarting) — it keeps running to completion even
        if every caller waiting on it times out."""
        project_key, language = key
        spawn_started = time.monotonic()
        try:
            # Dependency restore runs BEFORE the language server starts, and
            # deliberately runs inside this same background task rather than
            # as a separate step with its own timeout — get_or_start's
            # existing SPAWN_WAIT_BUDGET_SECONDS/StillStarting mechanism
            # already covers "this is taking a while, tell the caller to
            # retry" for the whole spawn, restore included. See
            # "Dependency resolution" in docs/lsp-integration.md. A
            # restore failure is logged and swallowed here, not re-raised —
            # it must degrade dependency-code navigation only, never block
            # project-local code navigation (which doesn't need any of this).
            try:
                dep_scratch_dir = await restore_dependencies(language, project_key, repo_root)
            except RestoreError as exc:
                dep_scratch_dir = None
                log.warning(
                    f"[pool] Dependency restore failed for project={project_key} "
                    f"language={language}, continuing without it: {exc}"
                )
            log.info(f"[pool] Starting language server: project={project_key} language={language}")
            config = MultilspyConfig.from_dict({"code_language": ml_language})
            logger = MultilspyLogger()
            # Rust/C#/Java/Dart go through thin arm64-safe subclasses (see
            # arm64_lsp_overrides.py) instead of multilspy's own
            # LanguageServer.create() dispatch — they fall straight through
            # to multilspy's unmodified behavior on non-arm64 hosts, so this
            # is safe regardless of the host architecture. Java (Eclipse
            # JDT Language Server) is EPL-2.0-licensed; we only ever launch
            # its official unmodified upstream binary as a subprocess, same
            # as every other language here — see
            # "Licensing — Java / EPL-2.0" in docs/lsp-integration.md for the
            # licensing analysis. Ruby (Solargraph) needs no override at all — it has
            # no platform-specific manifest logic — so it falls through to
            # LanguageServer.create() like every other clean-dispatch
            # language. C/C++ (clangd) has no multilspy dispatch at all —
            # see clangd_language_server.py for the from-scratch subclass;
            # it needs no arm64-specific handling either, since clangd is
            # installed via apt (native arm64 Debian build) at image build
            # time, not downloaded/pinned per-architecture like Rust/Dart.
            # Swift (sourcekit-lsp) also has no multilspy dispatch at all —
            # see swift_language_server.py; it needs no arm64-specific
            # handling either (Apple publishes an official debian12-aarch64
            # toolchain tarball), but its full toolchain is by far the
            # largest per-language image cost paid so far (~3.3GB).
            if ml_language == Language.RUST:
                server = Arm64RustAnalyzer(config, logger, repo_root)
            elif ml_language == Language.CSHARP:
                server = Arm64OmniSharp(config, logger, repo_root)
                if dep_scratch_dir:
                    # OmniSharp's own internal MSBuild project load is a
                    # SEPARATE process from our `dotnet restore` above, and
                    # doesn't inherit that subprocess's -p: flags — it needs
                    # the identical intermediate-output redirect as ENV VARS
                    # instead (MSBuild reads unset properties from the
                    # environment). Without this, OmniSharp tries to write
                    # bin/obj into the read-only project mount and fails to
                    # load the project at all (confirmed via live
                    # validation — see "Dependency resolution" in
                    # docs/lsp-integration.md).
                    server.server.process_launch_info.env.update(
                        {
                            "BaseIntermediateOutputPath": f"{dep_scratch_dir}/",
                            "MSBuildProjectExtensionsPath": f"{dep_scratch_dir}/",
                        }
                    )
            elif ml_language == Language.JAVA:
                server = Arm64EclipseJDTLS(config, logger, repo_root)
            elif ml_language == Language.DART:
                server = Arm64DartLanguageServer(config, logger, repo_root)
            elif ml_language == CPP_LANGUAGE_ID:
                server = ClangdLanguageServer(config, logger, repo_root)
            elif ml_language == SWIFT_LANGUAGE_ID:
                server = SwiftLanguageServer(config, logger, repo_root)
            elif ml_language == Language.PYTHON:
                # dependency_restore.py installs this project's packages into
                # a project_key-namespaced directory (not a flat shared
                # site-packages — see its module docstring for why); jedi
                # only finds them if told about that directory explicitly,
                # via initializationOptions.workspace.extraPaths — see
                # python_language_server.py for the fix (also corrects an
                # upstream multilspy bug: its jedi initialize_params.json
                # template is actually a copy-paste of rust-analyzer's
                # settings and was never valid for jedi-language-server).
                server = DependencyAwareJediServer(
                    config, logger, repo_root, extra_paths=[dep_scratch_dir] if dep_scratch_dir else []
                )
            elif ml_language == Language.RUBY:
                server = LanguageServer.create(config, logger, repo_root)
                if dep_scratch_dir and os.path.isfile(os.path.join(repo_root, "Gemfile")):
                    # solargraph is launched as a bare global executable
                    # (`solargraph stdio`, see multilspy's solargraph.py)
                    # — NOT via `bundle exec` (confirmed via live
                    # validation that `bundle exec solargraph` fails
                    # outright: solargraph itself isn't a dependency
                    # declared in any project's own Gemfile). Its gem
                    # lookup is governed by GEM_PATH at process start, so
                    # we set that directly to the shared BUNDLE_PATH-
                    # derived gem directory instead — see
                    # dependency_restore.ruby_shared_gem_path(). It also
                    # shells out to `bundle` internally to enumerate a
                    # workspace's Gemfile.lock, so BUNDLE_APP_CONFIG must
                    # be set here too (else that internal `bundle` call
                    # tries to write .bundle/config into the read-only
                    # project mount, same failure our own restore step
                    # originally hit).
                    gem_path = ruby_shared_gem_path()
                    if gem_path:
                        server.server.process_launch_info.env["GEM_PATH"] = gem_path
                    server.server.process_launch_info.env["BUNDLE_APP_CONFIG"] = dep_scratch_dir
            elif ml_language == Language.KOTLIN:
                server = LanguageServer.create(config, logger, repo_root)
                # kotlin-language-server shells out to `gradle`/`mvn` itself
                # (to resolve a project's classpath) via a plain JVM
                # ProcessBuilder — and on this container's filesystem stack
                # (Docker overlay2 + our read-only project bind-mounts),
                # the JDK's default process-launch mechanism (posix_spawn)
                # fails outright with a spurious `error=13, Permission
                # denied`, even though the same command succeeds fine via a
                # plain fork+exec (confirmed live: identical
                # ProcessBuilder().directory(<ro-mounted-project>) call
                # fails under the JVM default, but succeeds immediately
                # under -Djdk.lang.Process.launchMechanism=FORK or =VFORK;
                # a Python subprocess with the same cwd/env never hits
                # this at all, so it's JDK/posix_spawn-specific, not a
                # real permissions problem). kotlin-language-server's own
                # launch script (bin/kotlin-language-server) forwards the
                # JAVA_OPTS env var straight into its JVM invocation, so
                # setting it here reaches the *inner* gradle/mvn-spawning
                # JVM too, not just the outer one.
                existing_java_opts = server.server.process_launch_info.env.get("JAVA_OPTS", "")
                launch_mechanism_opt = "-Djdk.lang.Process.launchMechanism=FORK"
                server.server.process_launch_info.env["JAVA_OPTS"] = (
                    f"{existing_java_opts} {launch_mechanism_opt}".strip()
                )
            else:
                server = LanguageServer.create(config, logger, repo_root)
            context_manager = server.start_server()
            await context_manager.__aenter__()

            elapsed = time.monotonic() - spawn_started
            async with self._lock:
                self._servers[key] = _PooledServer(server, context_manager, idle_timeout_minutes)
            log.info(
                f"[pool] Loaded language server (ready): project={project_key} language={language} "
                f"startup_seconds={elapsed:.1f}"
            )
            return server
        except Exception:
            elapsed = time.monotonic() - spawn_started
            log.warning(
                f"[pool] Failed to start language server: project={project_key} language={language} "
                f"after {elapsed:.1f}s"
            )
            raise
        finally:
            # Always release the in-flight marker, whether the spawn
            # succeeded or raised — a failed spawn must be retriable by the
            # next request rather than wedged forever as "still starting".
            async with self._lock:
                self._spawning.pop(key, None)

    async def sweep_idle(self) -> None:
        async with self._lock:
            expired_keys = [k for k, v in self._servers.items() if v.is_expired()]
            for key in expired_keys:
                pooled = self._servers.pop(key)
                log.info(
                    f"[pool] Unloading language server (idle {pooled.idle_seconds:.0f}s > "
                    f"{pooled.idle_timeout_minutes}min timeout): project={key[0]} language={key[1]}"
                )
                try:
                    await pooled.stop()
                except Exception as exc:  # noqa: BLE001 — best-effort teardown, never block the sweep
                    log.warning(f"[pool] Error stopping evicted server {key}: {exc}")

    def status(self) -> list[dict]:
        return [
            {
                "project_key": key[0],
                "language": key[1],
                "idle_seconds": round(pooled.idle_seconds, 1),
            }
            for key, pooled in self._servers.items()
        ]


pool = LanguageServerPool()


async def _idle_sweep_loop() -> None:
    while True:
        await asyncio.sleep(IDLE_SWEEP_INTERVAL_SECONDS)
        try:
            await pool.sweep_idle()
        except Exception as exc:  # noqa: BLE001 — never let the sweep loop die
            log.warning(f"[pool] Idle sweep error: {exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Logged once per container start so an operator tailing container logs
    # can immediately see what this image is capable of serving, without
    # cross-referencing the Dockerfile — the actual per-project/language
    # spawn/loaded/unloaded lifecycle is logged separately as it happens
    # (see LanguageServerPool._spawn / sweep_idle / get_or_start above).
    log.info(f"[pool] Installed language servers: {', '.join(sorted(INSTALLED_LANGUAGES))}")
    sweep_task = asyncio.create_task(_idle_sweep_loop())
    yield
    sweep_task.cancel()


app = FastAPI(lifespan=lifespan)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class ResolveRequest(BaseModel):
    project_key: str
    language:    str
    file_path:   str   # relative to the project's root_path, same convention as get_file
    line:        int   # 1-based, same convention as get_file/repo_structure
    character:   int   # 0-based column, per LSP convention
    kind:        str   # "definition" | "references" | "hover"


class ResolvedLocation(BaseModel):
    file_path:  str
    start_line: int
    start_char: int
    end_line:   int
    end_char:   int
    # Populated ONLY when this location resolves outside the project's own
    # root_path (i.e. into a dependency — see "Dependency resolution" in
    # docs/lsp-integration.md). get_file (the
    # MCP-facing tool, project_server.ts) reads exclusively from this
    # project's own Qdrant/Postgres index — it has no access to the live
    # filesystem and dependency source was never indexed (it isn't part of
    # the user's repo). Since THIS container is the only thing with live
    # filesystem access to the mounted dependency caches, external source
    # is read directly here and inlined, rather than requiring a second
    # round-trip the agent has no tool for. None for in-project locations
    # (get_file already covers those, no need to duplicate).
    source_snippet: Optional[str] = None


class ResolveResponse(BaseModel):
    kind:    str
    results: list[ResolvedLocation] = []
    hover:   Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "active_servers": pool.status()}


@app.post("/resolve", response_model=ResolveResponse)
async def resolve(req: ResolveRequest) -> ResolveResponse:
    if req.kind not in _KIND_HANDLERS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {sorted(_KIND_HANDLERS)}, got '{req.kind}'",
        )

    try:
        server = await pool.get_or_start(req.project_key, req.language)
    except StillStarting as exc:
        # 503 + Retry-After, NOT a generic timeout: the caller (project_server.ts)
        # turns this into an agent-facing "still starting, retry in Ns" message
        # instead of a bare "resolve_symbol failed" so the agent knows to retry
        # rather than concluding the tool is broken (see StillStarting docstring).
        retry_after = round(exc.retry_after_seconds)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(retry_after)},
            content={
                "detail": str(exc),
                "still_starting": True,
                "language": exc.language,
                "retry_after_seconds": exc.retry_after_seconds,
            },
        )
    # multilspy uses 0-based lines; our API convention (matching get_file/
    # repo_structure) is 1-based, so convert at the boundary.
    ml_line = req.line - 1

    if req.kind == "definition":
        locations = await server.request_definition(req.file_path, ml_line, req.character)
        return ResolveResponse(
            kind=req.kind,
            results=[_to_resolved_location(loc, server.repository_root_path) for loc in locations],
        )

    if req.kind == "references":
        locations = await server.request_references(req.file_path, ml_line, req.character)
        return ResolveResponse(
            kind=req.kind,
            results=[_to_resolved_location(loc, server.repository_root_path) for loc in locations],
        )

    # kind == "hover"
    hover = await server.request_hover(req.file_path, ml_line, req.character)
    hover_text = None
    if hover is not None:
        contents = hover.get("contents")
        hover_text = contents.get("value") if isinstance(contents, dict) else str(contents)
    return ResolveResponse(kind=req.kind, hover=hover_text)


# Lines of context read on either side of the resolved definition/reference
# range for external (dependency) locations — enough to see a function
# signature/docstring plus a few lines of body, without dumping an entire
# (possibly huge, e.g. decompiled) source file into the response.
_EXTERNAL_SNIPPET_CONTEXT_LINES = 15
_EXTERNAL_SNIPPET_MAX_CHARS = 4000


def _read_source_snippet(absolute_path: str, start_line: int, end_line: int) -> Optional[str]:
    """Read a few lines of context directly from disk around a resolved,
    OUT-OF-PROJECT location (dependency source in one of the mounted
    dep-cache volumes). Returns None on any failure — including the
    expected case of OmniSharp's `$metadata$/...`-style virtual paths for
    decompiled dependencies, which don't correspond to a real file on
    disk at all. A missing snippet just means the agent gets the location
    without inline context, not an error."""
    try:
        with open(absolute_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None

    lo = max(1, start_line - _EXTERNAL_SNIPPET_CONTEXT_LINES)
    hi = min(len(lines), end_line + _EXTERNAL_SNIPPET_CONTEXT_LINES)
    snippet = "".join(lines[lo - 1 : hi])
    if len(snippet) > _EXTERNAL_SNIPPET_MAX_CHARS:
        snippet = snippet[:_EXTERNAL_SNIPPET_MAX_CHARS] + "\n… (truncated)"
    return snippet


def _to_resolved_location(loc: dict, repo_root: str) -> ResolvedLocation:
    """Convert a multilspy Location (0-based lines) to our 1-based
    convention, and inline a source snippet when the location falls
    outside repo_root (see ResolvedLocation.source_snippet docstring)."""
    rng = loc["range"]
    relative_path = loc.get("relativePath") or loc.get("uri", "")
    start_line = rng["start"]["line"] + 1
    end_line = rng["end"]["line"] + 1

    snippet = None
    absolute_path = os.path.normpath(os.path.join(repo_root, relative_path))
    repo_root_norm = os.path.normpath(repo_root)
    is_external = os.path.commonpath([absolute_path, repo_root_norm]) != repo_root_norm
    if is_external:
        snippet = _read_source_snippet(absolute_path, start_line, end_line)

    return ResolvedLocation(
        file_path=relative_path,
        start_line=start_line,
        start_char=rng["start"]["character"],
        end_line=end_line,
        end_char=rng["end"]["character"],
        source_snippet=snippet,
    )
