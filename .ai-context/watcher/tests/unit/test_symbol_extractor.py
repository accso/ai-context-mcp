"""
Unit tests for symbol_extractor.py — tree-sitter symbol/call-edge extraction
feeding the symbol_index/call_edges tables that analyze_impact and the
identifier fast-path in semantic_search both depend on.
"""
from pathlib import Path

import pytest

import symbol_extractor

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "sample-repo"

pytestmark = pytest.mark.skipif(
    not symbol_extractor._TS_AVAILABLE,
    reason="tree-sitter-languages not installed in this environment",
)


def _read(relative_path: str) -> str:
    return (FIXTURES / relative_path).read_text()


# ── is_supported() ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("src/kotlin/UserService.kt", True),
    ("src/typescript/notification.service.ts", True),
    ("src/python/dispatcher.py", True),
    ("src/java/OrderService.java", True),
    ("src/csharp/OrderService.cs", True),
    ("src/vue/UserCard.vue", False),   # no tree-sitter grammar wired for Vue
    ("docs/small-note.md", False),     # no tree-sitter grammar wired for Markdown
])
def test_is_supported_matches_extension_coverage(path, expected):
    assert symbol_extractor.is_supported(path) is expected


# ── extract_symbols() per language ────────────────────────────────────────────

def test_extract_symbols_kotlin():
    symbols = symbol_extractor.extract_symbols(
        "src/kotlin/UserService.kt", _read("src/kotlin/UserService.kt")
    )
    names_kinds = {(s["symbol_name"], s["symbol_kind"]) for s in symbols}
    assert ("UserService", "class") in names_kinds
    assert ("User", "class") in names_kinds
    # KNOWN BUG, discovered by this test (not previously documented anywhere):
    # `interface UserRepository { ... }` is reported as symbol_kind "class",
    # not "interface". The tree-sitter-languages Kotlin grammar currently in
    # use emits a `class_declaration` node for interfaces too (no distinct
    # `interface_declaration` node the way _SYMBOL_NODE_TYPES["kotlin"]'s
    # own comment assumes) — _KIND_MAP has no way to disambiguate from the
    # node type alone. This assertion documents CURRENT (buggy) behavior so
    # it can't regress silently further; it should be updated to expect
    # "interface" if/when this is fixed (e.g. by inspecting the "interface"
    # keyword token among the node's children).
    assert ("UserRepository", "class") in names_kinds  # should be "interface"
    # Methods on UserService should report it as parent_symbol.
    find_by_id = next(s for s in symbols if s["symbol_name"] == "findById" and s["symbol_kind"] == "function")
    assert find_by_id["parent_symbol"] == "UserService"


def test_extract_symbols_typescript():
    symbols = symbol_extractor.extract_symbols(
        "src/typescript/notification.service.ts",
        _read("src/typescript/notification.service.ts"),
    )
    names_kinds = {(s["symbol_name"], s["symbol_kind"]) for s in symbols}
    assert ("NotificationService", "class") in names_kinds
    assert ("route", "method") in names_kinds
    assert ("handleOrderCreated", "method") in names_kinds


def test_extract_symbols_python():
    symbols = symbol_extractor.extract_symbols(
        "src/python/dispatcher.py", _read("src/python/dispatcher.py")
    )
    names = {s["symbol_name"] for s in symbols}
    assert names == {
        "route", "handle_order_created", "handle_order_cancelled", "handle_user_registered",
    }
    assert all(s["symbol_kind"] == "function" for s in symbols)
    assert all(s["parent_symbol"] is None for s in symbols)  # module-level, no enclosing class


def test_extract_symbols_java():
    symbols = symbol_extractor.extract_symbols(
        "src/java/OrderService.java", _read("src/java/OrderService.java")
    )
    names_kinds = {(s["symbol_name"], s["symbol_kind"]) for s in symbols}
    assert ("OrderService", "class") in names_kinds
    assert ("OrderRepository", "interface") in names_kinds
    assert ("Order", "record") in names_kinds


def test_extract_symbols_csharp():
    symbols = symbol_extractor.extract_symbols(
        "src/csharp/OrderService.cs", _read("src/csharp/OrderService.cs")
    )
    names_kinds = {(s["symbol_name"], s["symbol_kind"]) for s in symbols}
    assert ("MyApp.Services", "namespace") in names_kinds
    assert ("OrderService", "class") in names_kinds


def test_extract_symbols_unsupported_extension_returns_empty():
    assert symbol_extractor.extract_symbols("src/vue/UserCard.vue", _read("src/vue/UserCard.vue")) == []


def test_extract_symbols_signature_is_first_declaration_line_capped():
    symbols = symbol_extractor.extract_symbols(
        "src/kotlin/UserService.kt", _read("src/kotlin/UserService.kt")
    )
    user_service = next(s for s in symbols if s["symbol_name"] == "UserService")
    assert user_service["signature"].startswith("class UserService")
    assert len(user_service["signature"]) <= 200


# ── extract_symbols_and_calls(): call-edge extraction ─────────────────────────

def test_extract_calls_python_module_level_call_attributed_to_caller():
    # main.py calls route(...) inside main() — the call must be attributed
    # to "main" as the nearest enclosing symbol, not left as module-level.
    symbols, calls = symbol_extractor.extract_symbols_and_calls(
        "src/python/main.py", _read("src/python/main.py")
    )
    assert any(s["symbol_name"] == "main" for s in symbols)
    route_calls = [c for c in calls if c["callee_name"] == "route"]
    assert len(route_calls) == 1
    assert route_calls[0]["caller_symbol_name"] == "main"


def test_extract_calls_dispatcher_internal_calls_attributed_to_route():
    # dispatcher.py's route() calls handle_order_created/_cancelled/_registered
    # — each call should be attributed to "route" as the enclosing function.
    _, calls = symbol_extractor.extract_symbols_and_calls(
        "src/python/dispatcher.py", _read("src/python/dispatcher.py")
    )
    callees = {c["callee_name"] for c in calls if c["caller_symbol_name"] == "route"}
    assert callees == {
        "handle_order_created", "handle_order_cancelled", "handle_user_registered",
    }


def test_extract_calls_unsupported_extension_returns_empty():
    symbols, calls = symbol_extractor.extract_symbols_and_calls(
        "src/vue/UserCard.vue", _read("src/vue/UserCard.vue")
    )
    assert symbols == [] and calls == []


# ── _rightmost_identifier / _callee_name: member-access collapsing ────────────

def test_callee_name_collapses_member_access_to_rightmost_identifier():
    # user.getName() should attribute the call to "getName", not "user" —
    # this is the documented non-goal-aware behavior (name-based only, no
    # type resolution) that analyze_impact's traversal relies on.
    src = (
        "class Example:\n"
        "    def run(self):\n"
        "        user = get_user()\n"
        "        return user.getName()\n"
    )
    _, calls = symbol_extractor.extract_symbols_and_calls("example.py", src)
    callees = {c["callee_name"] for c in calls}
    assert "getName" in callees
    assert "get_user" in callees
    assert "user" not in callees
