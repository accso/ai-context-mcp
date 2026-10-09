"""
Unit tests for validate_config.py — profile auto-detection, LSP-language
derivation, source-cycle detection, and port-collision checking. All pure
(dict/filesystem-scan in, dict/list out) — no Postgres/network needed (the
_pg_* methods are explicitly out of scope for Tier 1, see the class's own
"Postgres helpers (best-effort, no hard deps)" section).
"""
from pathlib import Path

import pytest

import validate_config as vc


def _make_validator() -> vc.Validator:
    # Paths are never read/written by the methods under test here (_detect_*,
    # _allocate_ports) — only self._errors/_warnings/_infos are touched.
    return vc.Validator(Path("unused/config.yaml"), Path("unused"), Path("unused/.resolved.json"))


# ── _detect_profile(): single-language, monorepo, and unknown-repo cases ──────

def test_detect_profile_single_language_backend_only(tmp_path):
    (tmp_path / "Service.kt").write_text("class Service\n")
    components, monorepo, _detail, _lsp_counts = vc._detect_profile(tmp_path)
    assert monorepo is False
    assert components == [
        {"language": "kotlin", "layer": "backend", "framework": None, "build_system": None}
    ]


def test_detect_profile_kotlin_backend_plus_react_frontend_is_a_real_monorepo(tmp_path):
    # Regression coverage for the class of bug the structured profile object
    # fixed: a Kotlin+React repo must produce TWO distinct components (not
    # collapse into one ambiguous "mixed" string) — that's what let
    # context_assembler.ts's expandQueries() false-positive on non-frontend
    # repos before the fix.
    (tmp_path / "Service.kt").write_text("class Service\n")
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "^18.0.0"}}')
    (tmp_path / "App.tsx").write_text("export const App = () => null;\n")

    components, monorepo, _detail, _lsp_counts = vc._detect_profile(tmp_path)
    assert monorepo is True
    langs_layers = {(c["language"], c["layer"]) for c in components}
    assert langs_layers == {("kotlin", "backend"), ("typescript", "frontend")}
    frontend = next(c for c in components if c["language"] == "typescript")
    assert frontend["framework"] == "react"


def test_detect_profile_empty_repo_falls_back_to_unknown_mixed_not_monorepo(tmp_path):
    components, monorepo, _detail, _lsp_counts = vc._detect_profile(tmp_path)
    assert monorepo is False
    assert components == [
        {"language": "unknown", "layer": "mixed", "framework": None, "build_system": None}
    ]


def test_detect_profile_kotlin_multiplatform_marker_sets_mixed_layer(tmp_path):
    (tmp_path / "commonMain").mkdir()
    (tmp_path / "commonMain" / "Shared.kt").write_text("expect class Shared\n")
    components, _monorepo, _detail, _lsp_counts = vc._detect_profile(tmp_path)
    kotlin = next(c for c in components if c["language"] == "kotlin")
    assert kotlin["layer"] == "mixed"


def test_detect_profile_python_with_pyproject_sets_build_system(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "main.py").write_text("def main(): pass\n")
    components, _monorepo, _detail, _lsp_counts = vc._detect_profile(tmp_path)
    python = next(c for c in components if c["language"] == "python")
    assert python["build_system"] == "pyproject"


def test_detect_profile_go_with_go_mod_sets_build_system(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    (tmp_path / "main.go").write_text("package main\n")
    components, _monorepo, _detail, _lsp_counts = vc._detect_profile(tmp_path)
    go = next(c for c in components if c["language"] == "go")
    assert go["build_system"] == "gomod"


def test_detect_profile_ignores_noise_directories(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "Fake.kt").write_text("class ShouldNotCount\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "Fake2.kt").write_text("class AlsoShouldNotCount\n")
    components, _monorepo, _detail, _lsp_counts = vc._detect_profile(tmp_path)
    assert components == [
        {"language": "unknown", "layer": "mixed", "framework": None, "build_system": None}
    ]


# ── _detect_lsp_languages(): supported ∩ installed, sorted, count > 0 only ────

def test_detect_lsp_languages_empty_counts_returns_empty_list():
    assert vc._detect_lsp_languages({}) == []


def test_detect_lsp_languages_filters_zero_counts():
    assert vc._detect_lsp_languages({"python": 0, "go": 3}) == ["go"]


def test_detect_lsp_languages_filters_unsupported_language_names():
    # "cobol" isn't in LSP_SUPPORTED_LANGUAGES/LSP_INSTALLED_LANGUAGES at all
    # — a nonzero count for it must never leak into the enabled-languages list.
    assert vc._detect_lsp_languages({"cobol": 5, "rust": 2}) == ["rust"]


def test_detect_lsp_languages_returns_sorted():
    result = vc._detect_lsp_languages({"typescript": 1, "csharp": 1, "python": 1})
    assert result == sorted(result)
    assert result == ["csharp", "python", "typescript"]


# ── _detect_lsp_languages(): Java/EPL-2.0 requires explicit opt-in ────────────
# (Eclipse JDT Language Server is EPL-2.0-licensed and downloaded straight from
# Red Hat's servers at Docker-build time — see docs/lsp-integration.md's
# "Licensing" section. Unlike every other auto-detected language, Java must
# NOT be silently enabled just because .java files exist: that would make us
# the ones triggering/bundling the EPL-2.0 download by default, rather than
# it being the deploying user's own explicit, informed choice.)

def test_detect_lsp_languages_excludes_java_by_default_even_when_present():
    assert vc._detect_lsp_languages({"java": 5, "python": 2}) == ["python"]


def test_detect_lsp_languages_includes_java_when_epl_accepted():
    result = vc._detect_lsp_languages({"java": 5, "python": 2}, java_epl_accepted=True)
    assert result == ["java", "python"]


def test_detect_lsp_languages_java_epl_accepted_has_no_effect_without_java_files():
    assert vc._detect_lsp_languages({"python": 2}, java_epl_accepted=True) == ["python"]


# ── Validator._validate_project(): lsp.accept_java_epl_license opt-in gate ────
# End-to-end wiring check: the config field must actually flow from
# config.yaml through to the auto-detected enabled_languages, and the
# no-opt-in case must be visibly explained (not a silent gap) so a user
# with a Java project understands why Java code intelligence didn't turn on.

def _make_project_validator(tmp_path) -> vc.Validator:
    return vc.Validator(tmp_path / "config.yaml", tmp_path, tmp_path / ".resolved.json")


def _base_project_raw() -> dict:
    return {"name": "app", "display_name": "App", "root_path": "", "mcp": {"port": 7100}}


def test_validate_project_java_files_without_opt_in_disables_java_and_informs(tmp_path):
    (tmp_path / "Main.java").write_text("class Main {}\n")
    v = _make_project_validator(tmp_path)
    result = v._validate_project(_base_project_raw(), 0)
    assert result["lsp"]["enabled_languages"] == []
    assert any("accept_java_epl_license" in msg for msg in v._infos)


def test_validate_project_java_epl_license_accepted_enables_java(tmp_path):
    (tmp_path / "Main.java").write_text("class Main {}\n")
    v = _make_project_validator(tmp_path)
    raw = _base_project_raw()
    raw["lsp"] = {"accept_java_epl_license": True}
    result = v._validate_project(raw, 0)
    assert result["lsp"]["enabled_languages"] == ["java"]


def test_validate_project_no_java_files_opt_in_has_no_effect(tmp_path):
    (tmp_path / "main.py").write_text("def main(): pass\n")
    v = _make_project_validator(tmp_path)
    raw = _base_project_raw()
    raw["lsp"] = {"accept_java_epl_license": True}
    result = v._validate_project(raw, 0)
    assert result["lsp"]["enabled_languages"] == ["python"]


def test_validate_project_accept_java_epl_license_invalid_type_is_error(tmp_path):
    v = _make_project_validator(tmp_path)
    raw = _base_project_raw()
    raw["lsp"] = {"accept_java_epl_license": "yes"}
    v._validate_project(raw, 0)
    assert any("accept_java_epl_license" in msg for msg in v._errors)


# ── Validator._validate_project(): java_epl_pending resolved-output flag ──────
# ai-context-up.sh reads this flag (not the log stream) to know which
# projects to interactively prompt about enabling Java. It must be True iff
# Java files exist AND Java isn't already enabled by either opt-in path.

def test_validate_project_java_epl_pending_true_when_undetected_and_unaccepted(tmp_path):
    (tmp_path / "Main.java").write_text("class Main {}\n")
    v = _make_project_validator(tmp_path)
    result = v._validate_project(_base_project_raw(), 0)
    assert result["lsp"]["java_epl_pending"] is True


def test_validate_project_java_epl_pending_false_when_accepted_via_config(tmp_path):
    (tmp_path / "Main.java").write_text("class Main {}\n")
    v = _make_project_validator(tmp_path)
    raw = _base_project_raw()
    raw["lsp"] = {"accept_java_epl_license": True}
    result = v._validate_project(raw, 0)
    assert result["lsp"]["java_epl_pending"] is False


def test_validate_project_java_epl_pending_false_when_no_java_files(tmp_path):
    (tmp_path / "main.py").write_text("def main(): pass\n")
    v = _make_project_validator(tmp_path)
    result = v._validate_project(_base_project_raw(), 0)
    assert result["lsp"]["java_epl_pending"] is False


# ── Validator._validate_project(): marker-file opt-in (written by
#    scripts/ai-context-up.sh after an interactive "enable Java?" prompt) ─────
# Mirrors the existing `.restore-choice-<project>` marker-file convention
# (see clean.sh/ai-context-up.sh) instead of programmatically rewriting the
# heavily-commented config.yaml.

def test_validate_project_java_epl_marker_file_enables_java(tmp_path):
    (tmp_path / "Main.java").write_text("class Main {}\n")
    (tmp_path / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".java-epl-accepted-app").write_text("")
    v = _make_project_validator(tmp_path)
    result = v._validate_project(_base_project_raw(), 0)
    assert result["lsp"]["enabled_languages"] == ["java"]
    assert result["lsp"]["java_epl_pending"] is False


def test_validate_project_java_epl_marker_file_is_project_scoped(tmp_path):
    # A marker file for a *different* project name must not leak acceptance.
    (tmp_path / "Main.java").write_text("class Main {}\n")
    (tmp_path / ".java-epl-accepted-other-project").write_text("")
    v = _make_project_validator(tmp_path)
    result = v._validate_project(_base_project_raw(), 0)
    assert result["lsp"]["enabled_languages"] == []
    assert result["lsp"]["java_epl_pending"] is True


# ── Validator._detect_cycles(): DFS cycle detection, reported as INFO ─────────

def test_detect_cycles_no_cycle_produces_no_info():
    v = _make_validator()
    projects = [
        {"name": "app", "_sources": [{"project": "lib", "kind": "local"}]},
        {"name": "lib", "_sources": []},
    ]
    v._detect_cycles(projects, {"app", "lib"})
    assert v._infos == []


def test_detect_cycles_direct_two_project_cycle_is_reported_once():
    v = _make_validator()
    projects = [
        {"name": "a", "_sources": [{"project": "b", "kind": "local"}]},
        {"name": "b", "_sources": [{"project": "a", "kind": "local"}]},
    ]
    v._detect_cycles(projects, {"a", "b"})
    assert len(v._infos) == 1
    assert "Source cycle detected" in v._infos[0]


def test_detect_cycles_self_reference_is_reported():
    v = _make_validator()
    projects = [{"name": "a", "_sources": [{"project": "a", "kind": "local"}]}]
    v._detect_cycles(projects, {"a"})
    assert len(v._infos) == 1


def test_detect_cycles_ignores_non_local_sources():
    v = _make_validator()
    projects = [
        {"name": "a", "_sources": [{"project": "external", "kind": "remote"}]},
    ]
    v._detect_cycles(projects, {"a"})
    assert v._infos == []


# ── Validator._allocate_ports(): collision detection ──────────────────────────

def test_allocate_ports_no_collision_produces_no_error():
    v = _make_validator()
    projects = [
        {"name": "a", "mcp": {"port": 7100}},
        {"name": "b", "mcp": {"port": 7101}},
    ]
    v._allocate_ports(projects)
    assert v._errors == []


def test_allocate_ports_collision_is_reported_as_error():
    v = _make_validator()
    projects = [
        {"name": "a", "mcp": {"port": 7100}},
        {"name": "b", "mcp": {"port": 7100}},
    ]
    v._allocate_ports(projects)
    assert len(v._errors) == 1
    assert "Port collision" in v._errors[0]
    assert "7100" in v._errors[0]
