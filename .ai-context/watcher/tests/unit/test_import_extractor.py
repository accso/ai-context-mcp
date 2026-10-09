"""
Unit tests for import_extractor.py — regex-based import/using statement
extraction and best-effort specifier resolution, feeding the import_edges
table (analyze_impact's "callers via import" heuristic, blast-radius test
detection).
"""
from pathlib import Path

import pytest

import import_extractor as ie

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "sample-repo"


def _read(relative_path: str) -> str:
    return (FIXTURES / relative_path).read_text()


# ── is_supported() ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("src/python/main.py", True),
    ("src/typescript/notification.service.ts", True),
    ("src/java/OrderService.java", True),
    ("src/csharp/OrderService.cs", True),
    ("src/kotlin/UserService.kt", True),
    ("src/vue/UserCard.vue", False),  # not in the six-language v1 subset
    ("docs/small-note.md", False),
])
def test_is_supported_matches_six_language_subset(path, expected):
    assert ie.is_supported(path) is expected


# ── extract_imports(): per-language patterns, against the fixture corpus ─────

def test_extract_imports_python_from_import():
    edges = ie.extract_imports("src/python/main.py", _read("src/python/main.py"))
    assert edges == [{"specifier": "dispatcher", "import_kind": "py_import", "line": 6}]


def test_extract_imports_typescript_named_import():
    edges = ie.extract_imports(
        "src/typescript/notification.service.ts",
        _read("src/typescript/notification.service.ts"),
    )
    assert {"specifier": "@angular/core", "import_kind": "es_import", "line": 1} in edges


def test_extract_imports_java_single_type_import():
    edges = ie.extract_imports("src/java/OrderService.java", _read("src/java/OrderService.java"))
    assert {"specifier": "java.util.Optional", "import_kind": "java_import", "line": 3} in edges


def test_extract_imports_csharp_using():
    edges = ie.extract_imports("src/csharp/OrderService.cs", _read("src/csharp/OrderService.cs"))
    assert {"specifier": "System", "import_kind": "using", "line": 1} in edges


def test_extract_imports_unsupported_extension_returns_empty():
    assert ie.extract_imports("src/vue/UserCard.vue", _read("src/vue/UserCard.vue")) == []


# ── extract_imports(): synthetic snippets for patterns not in the fixture corpus ─

def test_extract_imports_python_plain_import_multiple_specifiers():
    edges = ie.extract_imports("a.py", "import os, sys\n")
    assert edges == [
        {"specifier": "os", "import_kind": "py_import", "line": 1},
        {"specifier": "sys", "import_kind": "py_import", "line": 1},
    ]


def test_extract_imports_es_default_from_and_bare_and_export_from():
    src = (
        'import Foo from "./foo";\n'
        'import "./side-effect";\n'
        'export { Bar } from "./bar";\n'
    )
    edges = ie.extract_imports("a.ts", src)
    specifiers = [e["specifier"] for e in edges]
    assert specifiers == ["./foo", "./side-effect", "./bar"]
    assert all(e["import_kind"] == "es_import" for e in edges)


def test_extract_imports_cjs_require():
    edges = ie.extract_imports("a.js", 'const foo = require("./foo");\n')
    assert edges == [{"specifier": "./foo", "import_kind": "cjs_require", "line": 1}]


def test_extract_imports_kotlin_import():
    edges = ie.extract_imports("a.kt", "import com.example.other.Foo\n")
    assert edges == [{"specifier": "com.example.other.Foo", "import_kind": "kotlin_import", "line": 1}]


def test_extract_imports_swift_import():
    edges = ie.extract_imports("a.swift", "import Foundation\n")
    assert edges == [{"specifier": "Foundation", "import_kind": "swift_import", "line": 1}]


# ── resolve_import(): relative-path resolution ────────────────────────────────

def test_resolve_import_typescript_relative_specifier():
    known = {"src/foo/baz.ts", "src/foo/bar.ts"}
    resolved = ie.resolve_import("src/foo/bar.ts", "./baz", "es_import", known)
    assert resolved == "src/foo/baz.ts"


def test_resolve_import_typescript_relative_specifier_to_index_file():
    known = {"src/foo/utils/index.ts"}
    resolved = ie.resolve_import("src/foo/bar.ts", "./utils", "es_import", known)
    assert resolved == "src/foo/utils/index.ts"


def test_resolve_import_python_absolute_same_directory_specifier():
    # `from dispatcher import route` in main.py — same-directory bare-name
    # import, resolved via the absolute/module-specifier suffix-match path.
    known = {"src/python/dispatcher.py", "src/python/main.py"}
    resolved = ie.resolve_import("src/python/main.py", "dispatcher", "py_import", known)
    assert resolved == "src/python/dispatcher.py"


def test_resolve_import_python_relative_dot_syntax():
    # `from . import dispatcher` inside src/python/main.py.
    known = {"src/python/dispatcher.py", "src/python/main.py"}
    resolved = ie.resolve_import("src/python/main.py", ".dispatcher", "py_import", known)
    assert resolved == "src/python/dispatcher.py"


def test_resolve_import_java_dotted_package_suffix_match():
    # File layout mirrors the package path, as real Java projects do.
    known = {"src/main/java/com/example/fixture/OrderService.java"}
    resolved = ie.resolve_import(
        "src/main/java/com/example/fixture/Main.java",
        "com.example.fixture.OrderService",
        "java_import",
        known,
    )
    assert resolved == "src/main/java/com/example/fixture/OrderService.java"


def test_resolve_import_returns_none_for_external_unresolvable_specifier():
    # "os" (Python stdlib) never resolves to a project-local file — this is
    # the expected, common, non-error outcome for external/library imports.
    known = {"src/python/dispatcher.py", "src/python/main.py"}
    assert ie.resolve_import("src/python/main.py", "os", "py_import", known) is None


def test_resolve_import_unsupported_extension_returns_none():
    assert ie.resolve_import("a.vue", "./foo", "es_import", {"a/foo.vue"}) is None
