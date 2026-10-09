"""
symbol_extractor.py
Tree-sitter based AST symbol extraction for code files.

Extracts named symbols (classes, functions, interfaces, methods, …) from source files
and returns them as a list of dicts ready for storage in the `symbol_index` table.

Also extracts call-graph edges (see "Call & Import Graph" in
docs/architecture-deep-dive.md) in
the same tree walk — see extract_symbols_and_calls(). Each raw call is
attributed to its nearest enclosing symbol (or None for module-level calls);
name-to-symbol_index-ID resolution happens in indexer.py, which owns the DB
connection this module deliberately doesn't have.

Languages handled via tree-sitter-languages:
  Python, TypeScript, TSX, JavaScript, JSX, Java, C#, Kotlin, Swift

When tree-sitter is unavailable, or a file's language isn't in the list above
(Vue, SQL, YAML/TOML, Markdown, and — for now — C/C++, Go, Rust, Ruby, PHP),
extract_symbols() returns []. The actual fallback to chunker.py's
regex-derived symbol/chunk_type fields lives in indexer.py
(_fallback_symbols_from_chunks(), gated by is_supported() below) — not here.
It only covers languages chunker.py already has real structural boundary
detection for (Vue's SFC tags, YAML/TOML's top-level keys, Markdown's
headings, SQL's CREATE-statement names); C/C++, Go, Rust, Ruby, and PHP fall
through to chunker's generic paragraph splitter today, which assigns no
symbol at all, so the fallback produces nothing for those until chunker.py
gains real boundary detection for them (see chunker.py's module docstring).

Each returned dict:
  {
    "symbol_name":   str,
    "symbol_kind":   str,          # 'class' | 'function' | 'method' | 'interface' | …
    "parent_symbol": str | None,   # enclosing class/namespace, or None
    "signature":     str | None,   # first line of declaration (≤ 200 chars)
    "start_line":    int,          # 1-based
    "end_line":      int,
    "chunk_id":      None,         # filled in by indexer.py after chunk IDs are known
  }
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# ── tree-sitter availability check ────────────────────────────────────────────
try:
    from tree_sitter_languages import get_language, get_parser  # type: ignore
    _TS_AVAILABLE = True
except ImportError:
    _TS_AVAILABLE = False
    log.info(
        "tree-sitter-languages not installed — symbol extraction will use chunker symbols."
    )

# ── File extension → tree-sitter language name ────────────────────────────────
_EXT_TO_LANG: dict[str, str] = {
    ".py":   "python",
    ".ts":   "typescript",
    ".tsx":  "tsx",
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".java": "java",
    ".cs":   "c_sharp",
    ".kt":   "kotlin",
    ".kts":  "kotlin",
    ".swift": "swift",
}

# ── Node types to extract per language ────────────────────────────────────────
# Only top-level and class-member declarations are included; purely local
# function literals are intentionally skipped to keep the symbol table lean.
_SYMBOL_NODE_TYPES: dict[str, set[str]] = {
    "python": {
        "class_definition", "function_definition",
    },
    "typescript": {
        "class_declaration", "function_declaration", "method_definition",
        "interface_declaration", "enum_declaration", "type_alias_declaration",
    },
    "tsx": {
        "class_declaration", "function_declaration", "method_definition",
        "interface_declaration", "enum_declaration",
    },
    "javascript": {
        "class_declaration", "function_declaration", "method_definition",
    },
    "java": {
        "class_declaration", "interface_declaration", "method_declaration",
        "enum_declaration", "record_declaration", "constructor_declaration",
    },
    "c_sharp": {
        "class_declaration", "interface_declaration", "method_declaration",
        "struct_declaration", "enum_declaration", "namespace_declaration",
        "record_declaration", "constructor_declaration",
    },
    "kotlin": {
        "class_declaration", "function_declaration", "object_declaration",
        "interface_declaration", "secondary_constructor",
    },
    "swift": {
        "class_declaration", "function_declaration", "struct_declaration",
        "enum_declaration", "protocol_declaration", "extension_declaration",
        "initializer_declaration",
    },
}

# ── Call-expression node types per language (for call-edge extraction) ────────
# See "Call & Import Graph" in docs/architecture-deep-dive.md — reuses the
# same tree-sitter trees built for extract_symbols(), no separate re-parse.
_CALL_NODE_TYPES: dict[str, set[str]] = {
    "python":     {"call"},
    "typescript": {"call_expression"},
    "tsx":        {"call_expression"},
    "javascript": {"call_expression"},
    "java":       {"method_invocation"},
    "c_sharp":    {"invocation_expression"},
    "kotlin":     {"call_expression"},
    "swift":      {"call_expression"},
}

# ── Node-type → readable kind ─────────────────────────────────────────────────
_KIND_MAP: dict[str, str] = {
    "class_declaration":        "class",
    "class_definition":         "class",
    "function_declaration":     "function",
    "function_definition":      "function",
    "method_declaration":       "method",
    "method_definition":        "method",
    "constructor_declaration":  "constructor",
    "secondary_constructor":    "constructor",
    "initializer_declaration":  "initializer",
    "interface_declaration":    "interface",
    "enum_declaration":         "enum",
    "struct_declaration":       "struct",
    "record_declaration":       "record",
    "namespace_declaration":    "namespace",
    "object_declaration":       "object",
    "protocol_declaration":     "protocol",
    "extension_declaration":    "extension",
    "type_alias_declaration":   "typealias",
}


def is_supported(relative_path: str) -> bool:
    """
    Whether extract_symbols() has a real tree-sitter grammar for this file's
    extension (tree-sitter itself being installed notwithstanding). Used by
    indexer.py to decide whether an empty extract_symbols() result means
    "this language genuinely has no symbols in this file" (still authoritative
    — no fallback needed) vs. "this language isn't covered at all" (fall back
    to chunker-derived symbols instead — see indexer.py's
    _fallback_symbols_from_chunks).
    """
    ext = Path(relative_path).suffix.lower()
    return _TS_AVAILABLE and ext in _EXT_TO_LANG and bool(_SYMBOL_NODE_TYPES.get(_EXT_TO_LANG[ext]))


def extract_symbols(relative_path: str, content: str) -> list[dict]:
    """
    Extract named symbols from source content using tree-sitter AST parsing.
    Returns an empty list when tree-sitter is unavailable or the language is
    not supported — the caller falls back to the simpler chunker.symbol field.

    Thin wrapper around extract_symbols_and_calls() for callers that don't
    need call-edge data (kept for backward compatibility / clarity at call
    sites that only care about symbols).
    """
    symbols, _calls = extract_symbols_and_calls(relative_path, content)
    return symbols


def extract_symbols_and_calls(relative_path: str, content: str) -> tuple[list[dict], list[dict]]:
    """
    Extract named symbols AND call-expression edges in a single tree-sitter
    parse/walk (see "Call & Import Graph" in docs/architecture-deep-dive.md —
    do not re-parse
    for calls once symbols have already been walked).

    Returns (symbols, raw_calls) where each raw_calls entry is:
      {
        "caller_symbol_name": str | None,   # nearest enclosing symbol, None = module-level
        "caller_start_line":  int | None,   # disambiguates same-named symbols in-file
        "callee_name":        str,          # raw identifier text at the call site
        "call_line":          int,          # 1-based
      }
    Resolution of these names to symbol_index IDs happens in indexer.py, which
    has DB access; this module stays DB-free.
    """
    if not _TS_AVAILABLE:
        return [], []

    ext = Path(relative_path).suffix.lower()
    lang_name = _EXT_TO_LANG.get(ext)
    if not lang_name:
        return [], []

    node_types = _SYMBOL_NODE_TYPES.get(lang_name, set())
    if not node_types:
        return [], []

    call_node_types = _CALL_NODE_TYPES.get(lang_name, set())

    try:
        parser = get_parser(lang_name)
        tree   = parser.parse(content.encode("utf-8", errors="replace"))
        lines  = content.splitlines()
        symbols: list[dict] = []
        calls:   list[dict] = []
        _walk(tree.root_node, node_types, call_node_types, lang_name, lines, symbols, calls, parent=None)
        return symbols, calls
    except Exception as exc:
        log.warning(f"tree-sitter extraction failed for {relative_path}: {exc}")
        return [], []


def _walk(
    node,
    node_types:      set[str],
    call_node_types: set[str],
    lang_name:       str,
    lines:           list[str],
    results:         list[dict],
    calls:           list[dict],
    parent:          tuple[str, int] | None,   # (symbol_name, start_line) of nearest enclosing symbol
) -> None:
    """Recursive tree walk that collects symbol nodes and, in the same pass,
    call-expression nodes attributed to the nearest enclosing symbol."""
    if node.type in node_types:
        name_node = node.child_by_field_name("name")
        if name_node:
            symbol_name = name_node.text.decode("utf-8", errors="replace") if name_node.text else ""
            start_line  = node.start_point[0] + 1   # tree-sitter is 0-based
            end_line    = node.end_point[0]   + 1
            kind        = _KIND_MAP.get(node.type, node.type)

            # Signature = first non-empty source line of the declaration (≤ 200 chars)
            signature: str | None = None
            if 1 <= start_line <= len(lines):
                sig_raw   = lines[start_line - 1].strip()
                signature = sig_raw[:200] if sig_raw else None

            results.append({
                "symbol_name":   symbol_name,
                "symbol_kind":   kind,
                "parent_symbol": parent[0] if parent else None,
                "signature":     signature,
                "start_line":    start_line,
                "end_line":      end_line,
                "chunk_id":      None,  # filled by indexer.py
            })
            new_parent = (symbol_name, start_line)
        else:
            new_parent = parent
    else:
        new_parent = parent

    if node.type in call_node_types:
        callee_name = _callee_name(node, lang_name)
        if callee_name:
            calls.append({
                "caller_symbol_name": new_parent[0] if new_parent else None,
                "caller_start_line":  new_parent[1] if new_parent else None,
                "callee_name":        callee_name,
                "call_line":          node.start_point[0] + 1,
            })

    for child in node.children:
        _walk(child, node_types, call_node_types, lang_name, lines, results, calls, new_parent)


def _callee_name(call_node, lang_name: str) -> str | None:
    """Best-effort extraction of the identifier text naming the call target,
    e.g. "foo" for foo(), "getName" for user.getName() — member/attribute
    access is collapsed to its rightmost identifier since resolution is
    name-based only (see §1.3's explicit non-goal of full type resolution)."""
    if lang_name == "java":
        # method_invocation has a direct "name" field regardless of receiver.
        name_node = call_node.child_by_field_name("name")
        return _text(name_node)

    func_node = call_node.child_by_field_name("function")
    if func_node is None:
        # Kotlin/Swift call_expression nodes don't always expose a "function"
        # field under this grammar version — fall back to the first named child.
        named = [c for c in call_node.children if c.is_named]
        func_node = named[0] if named else None
    if func_node is None:
        return None
    return _rightmost_identifier(func_node)


def _rightmost_identifier(node) -> str | None:
    """Descend into member/attribute/navigation-style expressions to find the
    final identifier (the actual called name), e.g. a.b.c() -> "c"."""
    if node.type in (
        "identifier", "simple_identifier", "type_identifier",
        "property_identifier", "field_identifier",
    ):
        return _text(node)

    for field in ("attribute", "property", "name", "selector"):
        child = node.child_by_field_name(field)
        if child is not None:
            return _rightmost_identifier(child)

    named_children = [c for c in node.children if c.is_named]
    if named_children:
        return _rightmost_identifier(named_children[-1])
    return None


def _text(node) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode("utf-8", errors="replace")

