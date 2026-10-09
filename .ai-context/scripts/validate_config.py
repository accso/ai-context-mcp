#!/usr/bin/env python3
"""
validate_config.py — AI Context multi-project config validator.

Usage:
  python3 validate_config.py [--config PATH] [--repo-root PATH] [--output PATH]

Exit codes:
  0  OK (no errors, no warnings)
  1  Error(s) found — do NOT proceed with start.sh
  2  Warnings only — safe to proceed, but review the warnings
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed.  Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────

VALID_MODES: set[str] = {"l0", "l0+l1", "l0+l1+l2"}
KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# ── LSP bridge (see docs/lsp-integration.md) ─────────────
# LSP_SUPPORTED_LANGUAGES: the full set of languages supported.
# "kotlin" means `kotlin-language-server` (fwcd), not the newer JetBrains
# `kotlin-lsp`, still Alpha — see docs/lsp-integration.md before re-evaluating.
# "java" and
# "go"/"dart" are all licensing exceptions to the original
# MIT/Apache-2.0 constraint (EPL-2.0 and BSD-3-Clause respectively), each
# shipped only after an explicit project-owner decision — see "Licensing" in
# docs/lsp-integration.md
# for the licensing analysis and decision record before adding another
# exception language here. "cpp" (clangd) and "swift" (sourcekit-lsp) are
# both Apache-2.0 (clangd: WITH LLVM-exception) — strictly as-or-more
# permissive than plain Apache-2.0, no exception decision needed (see that
# doc). Note "swift" pulls in the full Swift toolchain (~3.3GB unpacked) —
# by far the largest per-language image cost of any language here.
#
# LSP_INSTALLED_LANGUAGES: the subset the Dockerfile is *capable of*
# installing right now — i.e. what the running bridge container CAN serve
# if at least one registered project needs it. This is no longer "always
# installed": since the conditional-install change,
# each build only actually installs the toolchains in the union of every
# registered project's enabled_languages (computed by start.sh and passed
# as the Dockerfile's LSP_LANGUAGES build-arg) — a language server binary
# is present in a given running image if and only if some project needed
# it at build time, even though it's listed here. Update this set (and the
# Dockerfile's gating case blocks) together, one language at a time, per
# the plan's Phase 3 rollout order. There is deliberately no per-project
# config field for this anymore (see _detect_lsp_languages) — a project's
# enabled languages are auto-derived from its own files intersected with
# this set, so rolling out a new language to every already-onboarded
# project is just "add the language server to the Dockerfile + this set",
# not a config edit anyone could forget or get wrong.
LSP_SUPPORTED_LANGUAGES: set[str] = {
    "typescript", "python", "go", "rust", "csharp", "kotlin", "java", "dart", "ruby", "cpp", "swift",
}
LSP_INSTALLED_LANGUAGES: set[str] = {
    "typescript", "python", "kotlin", "rust", "csharp", "java", "go", "dart", "ruby", "cpp", "swift",
}
# Languages that must NEVER be silently auto-enabled from a file census alone
# — even though they're in LSP_SUPPORTED_LANGUAGES/LSP_INSTALLED_LANGUAGES —
# because doing so would make this project the one triggering/bundling the
# download of their (non-permissively-licensed) language server by default.
# Currently just "java" (EPL-2.0 Eclipse JDT Language Server): BSD-3-Clause
# (go/dart) is permissive enough to need no per-deployment consent (see
# "Licensing" in docs/lsp-integration.md), unlike EPL-2.0's weak copyleft.
# Enabling one of these requires the deploying user's own explicit
# config.yaml opt-in (`lsp.accept_java_epl_license: true`) — see
# _detect_lsp_languages() and _validate_project() below, and
# THIRD-PARTY-NOTICES.md for why this distinction exists.
LSP_LANGUAGES_REQUIRING_EXPLICIT_OPT_IN: set[str] = {"java"}
DEFAULT_LSP_IDLE_TIMEOUT_MINUTES = 15

# ── Profile auto-detection ─────────────────────────────────────────────────────
#
# `profile` is no longer a config.yaml field — it is always derived from the
# project's actual files at validate time (see _detect_profile below) and
# written into .resolved.json as a structured object (see "Profile
# auto-detection" in docs/configuration.md), not a flat enum string. This
# keeps the value in sync with reality instead of drifting from a value
# someone typed once and never updated, and supports monorepos containing
# more than one language/layer.

_NOISE_DIR_NAMES = {
    ".git", "node_modules", "dist", "build", "target", "out", "bin",
    "__pycache__", ".venv", "venv", ".gradle", ".idea", ".vscode",
    "coverage", ".next", ".turbo", ".cache",
}

# Minimal extension→language map for census purposes only (deliberately not
# reusing watcher/symbol_extractor.py's _EXT_TO_LANG to avoid coupling this
# host-side, dependency-light script to the watcher's tree-sitter imports).
_LANG_EXTENSIONS: dict[str, str] = {
    ".kt": "kotlin", ".kts": "kotlin",
    ".java": "java",
    ".ts": "typescript", ".tsx": "typescript",
    ".vue": "vue",
}

# Extension→language map for LSP auto-detection (_detect_lsp_languages) AND
# for the structured profile's backend-language components (python/go/rust/
# csharp) — one shared file census serves both purposes, so a project's real
# language mix is read off directly without a second file walk.
_LSP_LANG_EXTENSIONS: dict[str, str] = {
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "typescript", ".jsx": "typescript",  # typescript-language-server also serves plain JS
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".cs": "csharp",
    ".kt": "kotlin", ".kts": "kotlin",
    ".java": "java",
    ".dart": "dart",
    ".rb": "ruby",
    ".c": "cpp", ".h": "cpp",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".swift": "swift",
}

_FRONTEND_DEP_MARKERS: dict[str, str] = {
    "react": "react",
    "@angular/core": "angular",
    "vue": "vue",
}

_MAX_SCAN_FILES = 20_000  # safety cap so a huge/misconfigured root_path can't hang validation


def _detect_profile(abs_root: Path) -> tuple[list[dict], bool, str, dict[str, int]]:
    """Infer a project's structured profile by scanning its files.

    Returns (components, monorepo, detail, lsp_lang_counts):
      - components: a list of {"language", "layer", "framework",
        "build_system"} dicts, one per detected language/layer. A plain
        single-stack repo has exactly one entry; a monorepo (e.g. a Kotlin
        backend + a React frontend) has one entry per domain — see
        "Profile auto-detection" in docs/configuration.md for the schema and
        the reasoning behind it (list-of-components over a
        primary+others shape, `layer` naming over `repo`, etc).
      - monorepo: components length > 1, kept as an explicit field for
        readability in logs/consumers rather than re-derived everywhere.
      - detail: a short human-readable summary suitable for an INFO log line.
      - lsp_lang_counts: a by-product of the same file walk — a file count
        per language in LSP_SUPPORTED_LANGUAGES, used by
        _detect_lsp_languages() (see docs/lsp-integration.md)
        AND reused here directly for the python/go/rust/csharp components,
        so both auto-detections share one os.walk.

    Detection is a best-effort heuristic — an unclear/empty repo falls back
    to a single {"language": "unknown", "layer": "mixed"} component, kept
    distinct from "monorepo" (2+ real domains detected) since those are
    different situations that used to be conflated under one "mixed" value.
    """
    ext_counts: dict[str, int] = {}
    lsp_ext_counts: dict[str, int] = {}
    has_gradle = False
    has_maven = False
    has_multiplatform_marker = False
    has_pyproject = False
    has_go_mod = False
    has_cargo_toml = False
    has_csproj = False
    frontend_deps: set[str] = set()
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(abs_root):
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIR_NAMES and not d.startswith(".")]

        if any(d in ("commonMain", "androidMain", "iosMain") for d in dirnames):
            has_multiplatform_marker = True

        for fname in filenames:
            scanned += 1
            if scanned > _MAX_SCAN_FILES:
                break

            ext = Path(fname).suffix.lower()
            if ext in _LANG_EXTENSIONS:
                lang = _LANG_EXTENSIONS[ext]
                ext_counts[lang] = ext_counts.get(lang, 0) + 1
            if ext in _LSP_LANG_EXTENSIONS:
                lsp_lang = _LSP_LANG_EXTENSIONS[ext]
                lsp_ext_counts[lsp_lang] = lsp_ext_counts.get(lsp_lang, 0) + 1
            if ext == ".csproj":
                has_csproj = True

            if fname in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"):
                has_gradle = True
                try:
                    text = (Path(dirpath) / fname).read_text(errors="ignore")
                    if "multiplatform" in text:
                        has_multiplatform_marker = True
                except OSError:
                    pass
            elif fname == "pom.xml":
                has_maven = True
            elif fname == "package.json":
                try:
                    pkg = json.loads((Path(dirpath) / fname).read_text(errors="ignore"))
                    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                    frontend_deps.update(deps.keys())
                except (OSError, json.JSONDecodeError):
                    pass
            elif fname in ("pyproject.toml", "requirements.txt", "setup.py"):
                has_pyproject = True
            elif fname == "go.mod":
                has_go_mod = True
            elif fname == "Cargo.toml":
                has_cargo_toml = True

        if scanned > _MAX_SCAN_FILES:
            break

    has_kotlin = ext_counts.get("kotlin", 0) > 0 or has_gradle
    has_java = ext_counts.get("java", 0) > 0 or has_maven
    frontend_framework = next(
        (label for dep, label in _FRONTEND_DEP_MARKERS.items() if dep in frontend_deps),
        None,
    )
    has_frontend = (
        frontend_framework is not None
        or ext_counts.get("typescript", 0) > 0
        or ext_counts.get("vue", 0) > 0
    )

    detail = (
        f"kotlin_files={ext_counts.get('kotlin', 0)} java_files={ext_counts.get('java', 0)} "
        f"ts_files={ext_counts.get('typescript', 0)} gradle={has_gradle} maven={has_maven} "
        f"frontend_deps={sorted(frontend_deps & set(_FRONTEND_DEP_MARKERS))} "
        f"lsp_lang_counts={dict(sorted(lsp_ext_counts.items()))}"
    )

    components: list[dict] = []

    if has_kotlin:
        components.append({
            "language": "kotlin",
            "layer": "mixed" if (has_multiplatform_marker and has_kotlin) else "backend",
            "framework": None,
            "build_system": "gradle" if has_gradle else None,
        })
    if has_java:
        components.append({
            "language": "java",
            "layer": "backend",
            "framework": None,
            "build_system": "maven" if has_maven else ("gradle" if has_gradle else None),
        })
    if has_frontend:
        components.append({
            "language": "typescript",
            "layer": "frontend",
            "framework": frontend_framework,
            "build_system": "npm",
        })
    # python/go/rust/csharp: reuse the LSP language census directly (same
    # file walk, see docstring) rather than a second set of ext_counts.
    if lsp_ext_counts.get("python", 0) > 0:
        components.append({
            "language": "python", "layer": "backend", "framework": None,
            "build_system": "pyproject" if has_pyproject else None,
        })
    if lsp_ext_counts.get("go", 0) > 0:
        components.append({
            "language": "go", "layer": "backend", "framework": None,
            "build_system": "gomod" if has_go_mod else None,
        })
    if lsp_ext_counts.get("rust", 0) > 0:
        components.append({
            "language": "rust", "layer": "backend", "framework": None,
            "build_system": "cargo" if has_cargo_toml else None,
        })
    if lsp_ext_counts.get("csharp", 0) > 0:
        components.append({
            "language": "csharp", "layer": "backend", "framework": None,
            "build_system": "csproj" if has_csproj else None,
        })

    if not components:
        components = [{"language": "unknown", "layer": "mixed", "framework": None, "build_system": None}]

    monorepo = len(components) > 1
    return components, monorepo, detail, lsp_ext_counts


def _detect_lsp_languages(lsp_ext_counts: dict[str, int], java_epl_accepted: bool = False) -> list[str]:
    """Auto-derive resolve_symbol's enabled languages for a project.

    A language is enabled iff the project actually contains at least one
    file of that language AND the bridge's Docker image is currently
    *capable of* installing that language's server (LSP_INSTALLED_LANGUAGES
    — see that set's own comment: it's a ceiling of what CAN be installed,
    not a guarantee every running image has it; start.sh only actually
    installs the union of languages every registered project ends up
    enabling here). This replaces a hand-maintained `lsp.enabled_languages`
    config field: rolling out a new language (Phase 3 of the plan) only
    requires adding its server to the Dockerfile and to
    LSP_INSTALLED_LANGUAGES — every already-onboarded project picks it up
    automatically on the next validate/start, with no config.yaml edits
    (and no chance of someone enabling a language whose server isn't
    actually installable, or forgetting to enable one that is).

    Exception: languages in LSP_LANGUAGES_REQUIRING_EXPLICIT_OPT_IN (i.e.
    "java") are excluded unless java_epl_accepted is True, regardless of
    file counts — see that set's own comment for why.
    """
    return sorted(
        lang for lang, count in lsp_ext_counts.items()
        if count > 0 and lang in LSP_SUPPORTED_LANGUAGES and lang in LSP_INSTALLED_LANGUAGES
        and (lang not in LSP_LANGUAGES_REQUIRING_EXPLICIT_OPT_IN or java_epl_accepted)
    )


def _java_epl_marker_path(config_path: Path, project_name: str) -> Path:
    """Path to the marker file that persists an interactive Java/EPL-2.0 opt-in.

    Written by scripts/ai-context-up.sh after the user answers "yes" to the
    "enable Java support?" prompt (see _validate_project's java_epl_pending
    flag), so the choice survives future `validate`/`start` runs without
    programmatically rewriting the heavily-commented config.yaml. Mirrors the
    existing `.restore-choice-<project>` marker-file convention (clean.sh /
    ai-context-up.sh), living alongside config.yaml (config_path.parent).
    """
    return config_path.parent / f".java-epl-accepted-{project_name}"


# ── Validator ─────────────────────────────────────────────────────────────────

class Validator:
    def __init__(self, config_path: Path, repo_root: Path, output_path: Path) -> None:
        self.config_path = config_path
        self.repo_root = repo_root.resolve()
        self.output_path = output_path
        self._errors: list[str] = []
        self._warnings: list[str] = []
        self._infos: list[str] = []

    # ── Public entry point ────────────────────────────────────────────────────

    def validate(self) -> int:
        """Run all validation checks. Returns exit code (0/1/2)."""
        data = self._load_yaml()
        if data is None:
            return 1

        # Old-schema guard
        if self._is_old_schema(data):
            self._error(
                "Old single-project config schema detected (has top-level 'project:' key).\n"
                "  Please replace your config.yaml with the new multi-project schema.\n"
                "  See .ai-context/config.yaml for the canonical template."
            )
            self._flush()
            return 1

        shared = self._validate_shared(data.get("shared") or {})
        projects = self._validate_projects_list(data.get("projects"))

        if self._errors:
            self._flush()
            return 1

        # Cross-project checks (need all names known first)
        local_names: set[str] = {p["name"] for p in projects}
        self._resolve_sources(projects, local_names)

        if self._errors:
            self._flush()
            return 1

        self._detect_cycles(projects, local_names)
        self._check_overlaps(projects)
        self._allocate_ports(projects)

        if self._errors:
            self._flush()
            return 1

        self._check_scopes(projects)

        # Build + write resolved output
        resolved = self._build_resolved(projects, shared)
        self._write_resolved(resolved)

        self._print_table(projects)
        self._flush()

        return 1 if self._errors else (2 if self._warnings else 0)

    # ── YAML loading ──────────────────────────────────────────────────────────

    def _load_yaml(self) -> Optional[dict]:
        try:
            with open(self.config_path) as f:
                data = yaml.safe_load(f)
        except FileNotFoundError:
            self._error(f"Config file not found: {self.config_path}")
            return None
        except yaml.YAMLError as exc:
            self._error(f"YAML parse error: {exc}")
            return None

        if not isinstance(data, dict):
            self._error("Config root must be a YAML mapping.")
            return None
        return data

    # ── Old-schema detection ──────────────────────────────────────────────────

    @staticmethod
    def _is_old_schema(data: dict) -> bool:
        return "project" in data and isinstance(data.get("project"), dict)

    # ── Shared block ──────────────────────────────────────────────────────────

    def _validate_shared(self, raw: dict) -> dict:
        llm = raw.get("llm") or {}
        idx = raw.get("indexer") or {}

        # Warn if someone still has the old backup section with deprecated keys
        bkp = raw.get("backup") or {}
        for deprecated_key in ("enabled", "change_cooldown_minutes", "periodic_minutes", "qdrant_format"):
            if deprecated_key in bkp:
                self._warn(
                    f"shared.backup.{deprecated_key} is no longer used. "
                    f"Backups are manual only — run: mise run ai-context-backup"
                )

        # ── LLM provider ──────────────────────────────────────────────────────
        llm_provider = str(llm.get("provider", "lmstudio"))
        if llm_provider not in ("lmstudio", "ollama", "omlx"):
            self._warn(
                f"shared.llm.provider '{llm_provider}' is not recognised. "
                f"Valid values: 'lmstudio', 'ollama', 'omlx'. Falling back to 'lmstudio'."
            )
            llm_provider = "lmstudio"

        _llm_base_urls = {
            "lmstudio": llm.get("lmstudio_base_url", "http://host.docker.internal:1234/v1"),
            "omlx":     llm.get("omlx_base_url",     "http://host.docker.internal:8000/v1"),
            "ollama":   "$(OLLAMA_URL)/v1",
        }

        if llm_provider == "omlx":
            self._info(
                "omlx provider configured — runs locally at 127.0.0.1:8000 (like LM Studio). "
                "No API key required for a default local setup. "
                "Set OMLX_API_KEY only if your omlx instance has authentication enabled."
            )

        # Resolve backup_output_dir to an absolute path
        backup_output_dir_raw = str(raw.get("backup_output_dir", ".ai-context/database-backup"))
        backup_dir = (self.repo_root / backup_output_dir_raw).resolve()

        backup_format = str(raw.get("backup_format", "both"))
        if backup_format not in ("both", "snapshot", "json"):
            self._warn(
                f"shared.backup_format '{backup_format}' is not recognised. "
                f"Valid values: 'both', 'snapshot', 'json'. Falling back to 'both'."
            )
            backup_format = "both"

        # ── Reranker ──────────────────────────────────────────────────────────
        # Backend/model are fixed at inference/Dockerfile build time, not
        # config.yaml — see config.yaml's own comment on this section. The
        # only remaining runtime knob is top_n.
        rnk = raw.get("reranker") or {}
        reranker = {
            "top_n": int(rnk.get("top_n", 20)),
        }

        # ── Skill file generation ─────────────────────────────────────────────
        # Config is a directory (or list of directories) where the canonical
        # repo-wide skill should be saved. init_ide.sh appends
        #   skills/ai-context-workflow/SKILL.md
        # under each configured directory.
        skill_raw = raw.get("skill") or {}

        def _normalise_skill_dir(value: object) -> str:
            if not value:
                return ""
            path = str(value).rstrip("/")
            if not path:
                return ""
            return path

        skill = {
            "target_path": _normalise_skill_dir(skill_raw.get("target_path", "") or ""),
            "target_paths": [
                _normalise_skill_dir(p)
                for p in (skill_raw.get("target_paths") or [])
                if p
            ],
        }

        # ── IDE MCP config save paths ───────────────────────────────────────────
        mcp_config_paths: list[str] = self._string_or_list(raw, "mcp_config_paths")

        return {
            "mcp_config_paths": mcp_config_paths,
            "mcp_config_paths": mcp_config_paths,
            "llm": {
                "provider":          llm_provider,
                "lmstudio_base_url": str(llm.get("lmstudio_base_url", "http://host.docker.internal:1234/v1")),
                "omlx_base_url":     str(llm.get("omlx_base_url",     "http://host.docker.internal:8000/v1")),
                "model":             str(llm.get("model", "qwen3-coder-30b")),
                "disable_thinking":  bool(llm.get("disable_thinking", False)),
            },
            "indexer": {
                "debounce_seconds":           float(idx.get("debounce_seconds", 2)),
                "reconcile_interval_minutes": int(idx.get("reconcile_interval_minutes", 30)),
                "polling_fallback_seconds":   float(idx.get("polling_fallback_seconds", 10)),
                "max_chunk_chars":            int(idx.get("max_chunk_chars", 6000)),
                "extra_skip_paths":           list(idx.get("extra_skip_paths") or []),
                "extra_gitignore_paths":      list(idx.get("extra_gitignore_paths") or []),
                "include_extensions":         list(
                    idx.get("include_extensions")
                    or [
                        ".kt", ".kts", ".ts", ".tsx", ".js", ".jsx",
                        ".vue", ".cs", ".java",
                        ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
                        ".go", ".rs", ".rb", ".php", ".swift",
                        ".py", ".sh", "Dockerfile",
                        ".html", ".css", ".scss", ".json", ".yaml", ".yml",
                        ".toml", ".sql", ".md",
                    ]
                ),
            },
            "doc_index": self._validate_doc_index(raw.get("doc_index") or {}),
            "reranker":       reranker,
            "skill":          skill,
            "mcp_config_paths": mcp_config_paths,
            "backup_dir":     str(backup_dir),
            "backup_format": backup_format,
        }

    @staticmethod
    def _string_or_list(raw: dict, key: str) -> list[str]:
        """Normalize a value that may be a string or a list of strings."""
        value = raw.get(key)
        if isinstance(value, list):
            return [str(p) for p in value if p]
        if value:
            return [str(value)]
        return []

    def _validate_doc_index(self, raw: dict) -> dict:
        """Validate and normalise the shared.doc_index block."""
        return {
            "enabled":                  bool(raw.get("enabled", True)),
            "tree_extensions":          list(raw.get("tree_extensions", [".md"])),
            "min_headings_for_tree":    int(raw.get("min_headings_for_tree", 8)),
            "max_tokens_per_node_summary": int(raw.get("max_tokens_per_node_summary", 300)),
            "tree_llm_model":           raw.get("tree_llm_model"),   # None = inherit
            "always_tree_paths":        list(raw.get("always_tree_paths", [
                "docs/", "wiki/", "adr/", "architecture/", "RFC/",
            ])),
        }

    # ── Projects list ─────────────────────────────────────────────────────────

    def _validate_projects_list(self, raw: object) -> list[dict]:
        if not raw:
            self._error("'projects' list is missing or empty.")
            return []
        if not isinstance(raw, list):
            self._error("'projects' must be a YAML list.")
            return []

        validated: list[dict] = []
        names_seen: set[str] = set()

        for i, item in enumerate(raw):
            p = self._validate_project(item, i)
            if p is None:
                continue
            if p["name"] in names_seen:
                self._error(f"Duplicate project name: '{p['name']}'")
            else:
                names_seen.add(p["name"])
                validated.append(p)

        return validated

    def _validate_project(self, raw: object, idx: int) -> Optional[dict]:
        if not isinstance(raw, dict):
            self._error(f"projects[{idx}] is not a mapping.")
            return None

        p = raw  # alias for readability
        result: dict = {}

        # ── name ──
        name = p.get("name")
        if not name or not isinstance(name, str):
            self._error(f"projects[{idx}].name is required and must be a non-empty string.")
            return None
        if not KEBAB_RE.match(name):
            self._warn(f"projects[{idx}] '{name}': name should be kebab-case (a-z, 0-9, hyphens).")
        result["name"] = name

        # ── display_name ──
        dn = p.get("display_name")
        if not dn or not isinstance(dn, str):
            self._error(f"projects[{idx}] '{name}': 'display_name' is required.")
            return None
        result["display_name"] = dn

        # ── root_path ──
        root_path_raw = p.get("root_path")
        if root_path_raw is None:
            self._error(f"projects[{idx}] '{name}': 'root_path' is required (use \"\" for repo root).")
            return None
        root_path = str(root_path_raw).strip().strip("/")

        if os.path.isabs(root_path_raw):
            self._error(
                f"projects[{idx}] '{name}': 'root_path' must be relative to the repo root, "
                f"not absolute (got: '{root_path_raw}')."
            )
            return None
        if ".." in Path(root_path).parts if root_path else False:
            self._error(
                f"projects[{idx}] '{name}': 'root_path' must not contain '..'"
            )
            return None
        abs_root = self.repo_root / root_path if root_path else self.repo_root
        if not abs_root.exists():
            self._error(
                f"projects[{idx}] '{name}': 'root_path' '{root_path or '(repo root)'}' "
                f"does not exist under {self.repo_root}."
            )
            return None
        result["root_path"] = root_path  # normalised (no leading/trailing slash)

        # ── profile (auto-detected, not a config field) ──
        if "profile" in p:
            self._error(
                f"projects[{idx}] '{name}': 'profile' is no longer a config.yaml field — "
                f"it is auto-detected from the project's files. Remove it from config.yaml."
            )
            return None
        components, monorepo, detail, lsp_ext_counts = _detect_profile(abs_root)
        component_summary = ", ".join(
            f"{c['language']}/{c['layer']}" + (f":{c['framework']}" if c.get("framework") else "")
            for c in components
        )
        self._info(
            f"projects[{idx}] '{name}': auto-detected profile "
            f"[{component_summary}]"
        )
        result["profile"] = {"components": components, "monorepo": monorepo}

        # ── mcp ──
        mcp_raw = p.get("mcp") or {}
        port = mcp_raw.get("port")
        if port is None:
            self._error(
                f"projects[{idx}] '{name}': 'mcp.port' is required. "
                f"Pick a free port unique to this machine (e.g. 7100)."
            )
            return None
        result["mcp"] = {
            "port":               int(port),
            "max_context_tokens":  int(mcp_raw.get("max_context_tokens", 8000)),
        }

        # ── sources ──
        sources_raw = p.get("sources") or []
        result["_sources"] = []
        for si, src in enumerate(sources_raw):
            vs = self._validate_source(src, name, si)
            if vs:
                result["_sources"].append(vs)

        # ── skill (optional per-project override of shared.skill) ──
        skill_raw = p.get("skill")
        if skill_raw:
            result["skill"] = {
                "target_path": str(skill_raw.get("target_path", "") or ""),
                "target_paths": [str(x) for x in (skill_raw.get("target_paths") or []) if x],
            }
        else:
            result["skill"] = None

        # ── indexer.extra_skip_paths (optional per-project ADDITION to
        #    shared.indexer.extra_skip_paths — not a replacement; merged in
        #    _build_resolved()) — e.g. for paths with sensitive or
        #    unnecessary data that only exist in this one project and
        #    shouldn't be a global exclusion for every other project. ──
        indexer_raw = p.get("indexer") or {}
        extra_skip_raw = indexer_raw.get("extra_skip_paths") or []
        if not isinstance(extra_skip_raw, list):
            self._error(f"projects[{idx}] '{name}': 'indexer.extra_skip_paths' must be a YAML list.")
            extra_skip_raw = []
        result["_extra_skip_paths"] = [str(s) for s in extra_skip_raw]

        # ── indexer.extra_gitignore_paths (optional per-project ADDITION to
        #    shared.indexer.extra_gitignore_paths — merged, not replaced, in
        #    _build_resolved()). Paths relative to the repo root (same
        #    convention as backup_output_dir), NOT necessarily inside this
        #    project's own root_path — e.g. a monorepo top-level .gitignore
        #    one or more directories above this project. Each resolved path
        #    is bind-mounted individually into the watcher container by
        #    start.sh, since the container otherwise only has this project's
        #    own root_path mounted and cannot see anything outside it. ──
        extra_gitignore_raw = indexer_raw.get("extra_gitignore_paths") or []
        if not isinstance(extra_gitignore_raw, list):
            self._error(f"projects[{idx}] '{name}': 'indexer.extra_gitignore_paths' must be a YAML list.")
            extra_gitignore_raw = []
        result["_extra_gitignore_paths"] = [str(s) for s in extra_gitignore_raw]

        # ── lsp (auto-detected, not a config field — see
        #    docs/lsp-integration.md). enabled_languages is derived from this
        #    project's actual files (see _detect_lsp_languages), the same
        #    "derive from reality, don't let it drift" principle as profile
        #    above. idle_timeout_minutes remains a (rarely needed) per-project
        #    override, since that's a performance knob, not a correctness-
        #    affecting choice. accept_java_epl_license is the one deliberate
        #    exception: Java is never auto-enabled from file counts alone
        #    (see LSP_LANGUAGES_REQUIRING_EXPLICIT_OPT_IN) because doing so
        #    would make this project the one triggering/bundling the
        #    EPL-2.0-licensed Eclipse JDT Language Server download by
        #    default — see "Licensing — Java / EPL-2.0" in
        #    docs/lsp-integration.md and THIRD-PARTY-NOTICES.md. ──
        lsp_raw = p.get("lsp") or {}
        if not isinstance(lsp_raw, dict):
            self._error(f"projects[{idx}] '{name}': 'lsp' must be a YAML mapping.")
            lsp_raw = {}
        if "enabled_languages" in lsp_raw:
            self._error(
                f"projects[{idx}] '{name}': 'lsp.enabled_languages' is no longer a "
                f"config.yaml field — it is auto-detected from the project's files "
                f"(intersected with the languages the LSP bridge image is capable "
                f"of installing). Remove it from config.yaml."
            )

        java_epl_accepted_raw = lsp_raw.get("accept_java_epl_license", False)
        if not isinstance(java_epl_accepted_raw, bool):
            self._error(
                f"projects[{idx}] '{name}': 'lsp.accept_java_epl_license' must be a boolean "
                f"(true/false). It opts in to downloading and running the EPL-2.0-licensed "
                f"Eclipse JDT Language Server directly from Red Hat's servers at Docker-build "
                f"time — see docs/lsp-integration.md."
            )
            java_epl_accepted_raw = False

        # Marker file written by scripts/ai-context-up.sh after an interactive
        # "enable Java support?" prompt (see _java_epl_marker_path) — mirrors
        # the existing `.restore-choice-<project>` convention rather than
        # rewriting the heavily-commented config.yaml programmatically. Either
        # this OR the config.yaml field above counts as acceptance.
        java_epl_marker_exists = _java_epl_marker_path(self.config_path, name).exists()
        java_epl_accepted = bool(java_epl_accepted_raw) or java_epl_marker_exists

        enabled_languages = _detect_lsp_languages(lsp_ext_counts, java_epl_accepted=java_epl_accepted)
        self._info(
            f"projects[{idx}] '{name}': auto-detected lsp languages "
            f"{enabled_languages or '[]'}"
        )
        java_epl_pending = lsp_ext_counts.get("java", 0) > 0 and not java_epl_accepted
        if java_epl_pending:
            self._info(
                f"projects[{idx}] '{name}': Java files detected but Java code "
                f"intelligence is disabled — it requires downloading the EPL-2.0-"
                f"licensed Eclipse JDT Language Server. Set "
                f"'lsp.accept_java_epl_license: true' in config.yaml to enable it. "
                f"See docs/lsp-integration.md."
            )


        idle_timeout_raw = lsp_raw.get("idle_timeout_minutes", DEFAULT_LSP_IDLE_TIMEOUT_MINUTES)
        try:
            idle_timeout_minutes = float(idle_timeout_raw)
            if idle_timeout_minutes <= 0:
                raise ValueError
        except (TypeError, ValueError):
            self._error(
                f"projects[{idx}] '{name}': 'lsp.idle_timeout_minutes' must be a positive number."
            )
            idle_timeout_minutes = DEFAULT_LSP_IDLE_TIMEOUT_MINUTES

        result["lsp"] = {
            "enabled_languages": enabled_languages,
            "idle_timeout_minutes": idle_timeout_minutes,
            "java_epl_pending": java_epl_pending,
        }

        return result

    def _validate_source(self, raw: object, project_name: str, si: int) -> Optional[dict]:
        if not isinstance(raw, dict):
            self._error(f"Project '{project_name}' sources[{si}] is not a mapping.")
            return None

        ref = raw.get("project")
        if not ref or not isinstance(ref, str):
            self._error(f"Project '{project_name}' sources[{si}].project is required.")
            return None

        mode = str(raw.get("mode") or "l0+l1")
        if mode not in VALID_MODES:
            self._error(
                f"Project '{project_name}' sources[{si}].mode '{mode}' is invalid. "
                f"Must be one of: {sorted(VALID_MODES)}"
            )
            return None

        return {
            "project": ref,
            "scope": str(raw.get("scope") or ""),
            "mode": mode,
            "when": str(raw.get("when") or ""),
            "kind": "local",  # resolved in _resolve_sources
        }

    # ── Source resolution ─────────────────────────────────────────────────────

    def _resolve_sources(self, projects: list[dict], local_names: set[str]) -> None:
        for p in projects:
            for src in p["_sources"]:
                ref = src["project"]
                if ref == p["name"]:
                    self._error(f"Project '{p['name']}' lists itself as a source (self-source).")
                    continue
                if ref in local_names:
                    src["kind"] = "local"
                else:
                    # Best-effort Postgres lookup (new schema: project_key column).
                    # NOTE: This query targets the Phase 2.3 schema (project_key column).
                    # Until Phase 2.3 is applied the query won't return results;
                    # the validator will warn and treat the source as external.
                    found = self._pg_project_exists(ref)
                    if found is False:
                        self._error(
                            f"Project '{p['name']}' references source '{ref}' "
                            f"which is not defined locally and not found in the DB."
                        )
                    else:
                        src["kind"] = "external" if found else "external"
                        if found is None:
                            self._warn(
                                f"Cannot reach Postgres to validate external source '{ref}' "
                                f"(referenced by '{p['name']}'). Treating as external."
                            )

    # ── Cycle detection ───────────────────────────────────────────────────────

    def _detect_cycles(self, projects: list[dict], local_names: set[str]) -> None:
        """DFS cycle detection on the local source graph. Cycles are INFO, not errors."""
        adj: dict[str, list[str]] = {
            p["name"]: [
                s["project"] for s in p["_sources"] if s["kind"] == "local"
            ]
            for p in projects
        }

        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {n: WHITE for n in local_names}
        path: list[str] = []
        reported: set[frozenset] = set()

        def dfs(node: str) -> None:
            color[node] = GRAY
            path.append(node)
            for nbr in adj.get(node, []):
                if nbr not in color:
                    continue
                if color[nbr] == GRAY:
                    cycle_start = path.index(nbr)
                    cycle = path[cycle_start:] + [nbr]
                    key = frozenset(cycle)
                    if key not in reported:
                        reported.add(key)
                        self._info(
                            f"Source cycle detected (depth-1 cap prevents loops): "
                            f"{' → '.join(cycle)}"
                        )
                elif color[nbr] == WHITE:
                    dfs(nbr)
            path.pop()
            color[node] = BLACK

        for node in list(local_names):
            if color[node] == WHITE:
                dfs(node)

    # ── Overlap check ─────────────────────────────────────────────────────────

    def _check_overlaps(self, projects: list[dict]) -> None:
        pairs = [(p["name"], p["root_path"]) for p in projects]
        for i, (an, ar) in enumerate(pairs):
            for j, (bn, br) in enumerate(pairs):
                if i >= j:
                    continue
                if _paths_overlap(ar, br):
                    ar_label = ar or "(repo root)"
                    br_label = br or "(repo root)"
                    self._warn(
                        f"Overlapping root_path between '{an}' ({ar_label!r}) "
                        f"and '{bn}' ({br_label!r}). "
                        f"Both projects will index overlapping files."
                    )

    # ── Port allocation ───────────────────────────────────────────────────────

    def _allocate_ports(self, projects: list[dict]) -> None:
        """Check for port collisions. All ports must be set explicitly."""
        used: dict[int, str] = {}
        for p in projects:
            port = p["mcp"]["port"]
            if port in used:
                self._error(
                    f"Port collision: '{used[port]}' and '{p['name']}' "
                    f"both use port {port}."
                )
            else:
                used[port] = p["name"]


    # ── Scope existence (best-effort Postgres) ────────────────────────────────

    def _check_scopes(self, projects: list[dict]) -> None:
        for p in projects:
            for src in p["_sources"]:
                if not src.get("scope"):
                    continue
                exists = self._pg_scope_exists(src["project"], src["scope"])
                if exists is False:
                    self._warn(
                        f"Project '{p['name']}' sources '{src['project']}' "
                        f"with scope='{src['scope']}' — no indexed files found under that prefix."
                    )

    # ── Postgres helpers (best-effort, no hard deps) ──────────────────────────

    def _pg_run(self, sql: str) -> Optional[str]:
        """Run a SQL query via docker exec. Returns stdout on success, None on any failure."""
        try:
            result = subprocess.run(
                [
                    "docker", "exec", "ai_context_postgres",
                    "psql", "-U", "ai_context", "-d", "ai_context",
                    "-t", "-A", "-c", sql,
                ],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _pg_project_exists(self, project_name: str) -> Optional[bool]:
        """True = found, False = not found, None = DB unreachable."""
        safe = project_name.replace("'", "''")
        out = self._pg_run(
            f"SELECT 1 FROM projects WHERE project_key = '{safe}' LIMIT 1;"
        )
        if out is None:
            return None
        return bool(out)

    def _pg_scope_exists(self, project_name: str, scope: str) -> Optional[bool]:
        """True = ≥1 file, False = 0 files, None = DB unreachable."""
        safe_proj = project_name.replace("'", "''")
        safe_scope = scope.replace("'", "''")
        out = self._pg_run(
            f"SELECT COUNT(*) FROM file_index "
            f"WHERE project_key = '{safe_proj}' "
            f"AND relative_path LIKE '{safe_scope}%';"
        )
        if out is None:
            return None
        try:
            return int(out) > 0
        except ValueError:
            return None

    # ── Resolved JSON ─────────────────────────────────────────────────────────

    def _build_resolved(self, projects: list[dict], shared: dict) -> dict:
        resolved_projects: dict[str, dict] = {}
        backup_dir_abs = Path(shared["backup_dir"])
        for p in projects:
            abs_root = (self.repo_root / p["root_path"]) if p["root_path"] else self.repo_root

            # Merge extra_skip_paths: shared list + this project's own additions
            # (see _validate_project's "_extra_skip_paths") + an automatic
            # exclusion of this project's own backup_output_dir, IF it happens
            # to fall inside this project's root_path (only relevant for a
            # project whose root_path is the repo root or an ancestor of the
            # configured backup dir — e.g. this repo's own self-hosted
            # 'ai-context' project, whose root_path IS '.ai-context', the same
            # directory backup_output_dir defaults into). Without this, a
            # project would silently index and search its own backup dumps
            # (raw Postgres/Qdrant exports) as if they were source code.
            extra_skip = list(shared["indexer"]["extra_skip_paths"]) + list(p.get("_extra_skip_paths") or [])
            try:
                backup_rel_to_root = backup_dir_abs.relative_to(abs_root)
                auto_entry = backup_rel_to_root.as_posix()
                if auto_entry not in extra_skip:
                    extra_skip.append(auto_entry)
            except ValueError:
                pass  # backup dir isn't inside this project's root — nothing to add

            # Merge extra_gitignore_paths: shared list + this project's own
            # additions (see _validate_project's "_extra_gitignore_paths"),
            # resolved to absolute host paths (relative to repo_root, same
            # convention as backup_output_dir). These are NOT required to be
            # inside this project's root_path — that's the whole point (e.g.
            # a monorepo top-level .gitignore) — so they cannot be read
            # directly by the watcher container from its /workspace mount.
            # start.sh bind-mounts each one individually into the container
            # at a deterministic path (see config_loader.extra_gitignore_container_paths());
            # order here must match the order start.sh iterates in.
            extra_gitignore_rel = list(shared["indexer"]["extra_gitignore_paths"]) + list(p.get("_extra_gitignore_paths") or [])
            extra_gitignore_abs: list[str] = []
            for rel in extra_gitignore_rel:
                abs_path = (self.repo_root / rel).resolve()
                if not abs_path.is_file():
                    self._warn(
                        f"Project '{p['name']}': indexer.extra_gitignore_paths entry "
                        f"'{rel}' does not resolve to an existing file at {abs_path} — "
                        f"it will be skipped."
                    )
                    continue
                extra_gitignore_abs.append(str(abs_path))

            resolved_projects[p["name"]] = {
                "name": p["name"],
                "display_name": p["display_name"],
                "profile": p["profile"],
                "root_path": p["root_path"],
                "mcp": p["mcp"],
                "llm": shared["llm"],
                "indexer": {
                    **shared["indexer"],
                    "extra_skip_paths":      extra_skip,
                    "extra_gitignore_paths": extra_gitignore_abs,
                },
                "doc_index": shared["doc_index"],
                "skill": p.get("skill") or shared["skill"],
                "lsp": p["lsp"],
                "sources": [
                    {
                        "project": s["project"],
                        "scope": s["scope"],
                        "mode": s["mode"],
                        "when": s["when"],
                        "kind": s["kind"],
                    }
                    for s in p["_sources"]
                ],
            }
        return {
            "config_path":      str(self.config_path),
            "repo_root":        str(self.repo_root),
            "backup_dir":       shared["backup_dir"],
            "backup_format":    shared["backup_format"],
            "reranker":         shared["reranker"],
            "skill":            shared["skill"],
            "mcp_config_paths": shared["mcp_config_paths"],
            "projects":         resolved_projects,
        }

    def _write_resolved(self, resolved: dict) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_path, "w") as f:
            json.dump(resolved, f, indent=2, sort_keys=True)
            f.write("\n")
        self._info(f"Wrote resolved config → {self.output_path}")

    # ── Output ────────────────────────────────────────────────────────────────

    def _print_table(self, projects: list[dict]) -> None:
        col_proj = max(len("PROJECT"), *(len(p["name"]) for p in projects)) + 2
        col_port = 6
        col_root = max(len("ROOT"), *(len(p["root_path"] or "(repo root)") for p in projects)) + 2

        header = (
            f"{'PROJECT':<{col_proj}}  {'PORT':<{col_port}}  {'ROOT':<{col_root}}  SOURCES"
        )
        print()
        print(header)
        print("─" * (len(header) + 10))

        for p in projects:
            sources_str = (
                ", ".join(
                    f"{s['project']}({s['mode']}"
                    + (f", scope={s['scope']}" if s.get("scope") else "")
                    + (f", kind={s['kind']}" if s.get("kind") != "local" else "")
                    + ")"
                    for s in p["_sources"]
                )
                or "—"
            )
            root_label = p["root_path"] or "(repo root)"
            print(
                f"{p['name']:<{col_proj}}  "
                f"{p['mcp']['port']:<{col_port}}  "
                f"{root_label:<{col_root}}  "
                f"{sources_str}"
            )

    def _flush(self) -> None:
        print()
        for msg in self._infos:
            print(msg)
        for msg in self._warnings:
            print(msg)
        for msg in self._errors:
            print(msg, file=sys.stderr)

    # ── Message helpers ───────────────────────────────────────────────────────

    def _error(self, msg: str) -> None:
        self._errors.append(f"ERROR: {msg}")

    def _warn(self, msg: str) -> None:
        self._warnings.append(f"WARN:  {msg}")

    def _info(self, msg: str) -> None:
        self._infos.append(f"INFO:  {msg}")


# ── Utilities ─────────────────────────────────────────────────────────────────

def _paths_overlap(a: str, b: str) -> bool:
    """True when path a and b overlap (one contains the other or they are equal)."""
    if a == b:
        return True
    # Normalise: treat "" as repo root "/"
    a_norm = (a.rstrip("/") + "/") if a else "/"
    b_norm = (b.rstrip("/") + "/") if b else "/"
    return a_norm.startswith(b_norm) or b_norm.startswith(a_norm)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate .ai-context/config.yaml and write .resolved.json"
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to config.yaml (default: .ai-context/config.yaml next to this script's parent)",
    )
    parser.add_argument(
        "--repo-root",
        metavar="PATH",
        help="Absolute path to the repo root (default: parent of .ai-context/)",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="Destination for .resolved.json (default: .ai-context/.resolved.json)",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent   # .ai-context/scripts/
    ai_context_dir = script_dir.parent              # .ai-context/

    repo_root = Path(args.repo_root).resolve() if args.repo_root else ai_context_dir.parent
    config_path = Path(args.config).resolve() if args.config else ai_context_dir / "config.yaml"
    output_path = Path(args.output).resolve() if args.output else ai_context_dir / ".resolved.json"

    v = Validator(config_path, repo_root, output_path)
    sys.exit(v.validate())


if __name__ == "__main__":
    main()



