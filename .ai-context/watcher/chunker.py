"""
chunker.py
Language-aware code chunker with structure-aware fallbacks for prose/config/data files.

  - Kotlin/KTS:        splits at class, object, fun, interface boundaries
  - TypeScript/TSX:    splits at class, function, const arrow-function, React component boundaries
  - JavaScript/JSX:    same as TypeScript
  - Python (.py):      splits at class/def boundaries (top-level)
  - C# (.cs):          splits at class, interface, enum, struct, record, namespace boundaries
  - Java (.java):      splits at class, interface, enum, record, @interface boundaries
  - Swift (.swift):    splits at class, struct, enum, protocol, func, extension, actor boundaries
  - Go (.go):          splits at func (incl. methods with a receiver) and top-level type boundaries
  - Rust (.rs):        splits at fn, struct, enum, trait, impl boundaries (top-level only)
  - Ruby (.rb):        splits at class, module, def boundaries (top-level only)
  - PHP (.php):        splits at class, interface, trait, function boundaries (top-level only)
  - C/C++ (.c/.cc/.cpp/.cxx/.h/.hpp): splits at struct/class/enum/union declarations and
                       function *definitions* (heuristic — see _C_FUNCTION's comment for
                       known limitations; this is the least regular syntax handled here)
  - Vue (.vue):        splits at top-level SFC blocks (<template>, <script>, <style>)
  - Markdown (.md):    tree-indexed when large (≥ min_headings_for_tree or in always_tree_paths);
                       otherwise splits at heading lines (# / ## / ###)
  - SQL (.sql):        splits at statement boundaries (terminated by ";"); CREATE TABLE/VIEW/
                       INDEX/FUNCTION/PROCEDURE/TRIGGER statements also get a "symbol"
  - YAML/TOML (.yaml, .yml, .toml): splits at top-level keys (non-indented "key:" lines)
  - Other files:       splits by blank-line paragraphs, max 80 lines per chunk

Dispatch-table sub-chunking (additional pass, TS/TSX/JS/JSX/Java/C#/Swift,
Kotlin, Python — see _DISPATCH_TABLE): switch/when/match blocks with >= 3
arms get their arms split into their own chunks (chunk_type="case") within
an already-produced function/class chunk, so a value computed inside one
arm becomes independently searchable/identifiable instead of being buried,
invisible, inside its enclosing function's single chunk.

Each chunk is a dict:
  {
    "text": str,         # the chunk content
    "start_line": int,   # 1-based
    "end_line": int,
    "symbol": str|None,  # bare declared name for AST-boundary languages (e.g. "UserService",
                         # not "class UserService" — see _chunk_by_pattern); for the bespoke
                         # chunkers it's the raw matched text instead (e.g. "## Usage",
                         # "<script setup>", "CREATE TABLE users"); for a dispatch arm
                         # (chunk_type="case") it's the arm's label, quotes stripped
                         # (e.g. `case "get_index_status":` -> "get_index_status")
    "chunk_type": str,   # "class"|"function"|"component"|"heading"|"statement"|"block"|"case"
  }

chunk_file() returns (list[dict], bool) where the bool is use_tree_index.
When use_tree_index is True the caller must NOT store chunks in Qdrant.
"""

import re
from pathlib import Path

import symbol_extractor  # reuses its tree-sitter parser setup + node-type
# tables for .cs/.java/.ts/.tsx tree-sitter chunk boundaries (§2.1) — see
# _chunk_by_treesitter() below. Deliberately a plain module import (not
# `from ... import ...`) so symbol_extractor._TS_AVAILABLE is read fresh at
# call time via attribute access, not captured once at import time.

CHUNKER_VERSION = 7  # bump this integer whenever chunk_file()'s boundary changes
# in a way that affects previously-stored start_line/end_line/text,
# so indexer.py knows to force a re-chunk even when content_sha256
# is unchanged. This value became 2 as part of the Plan D preamble-
# chunk fix (2026-07-22 chunker gaps plan).
#
# This is also indexer.py's ONLY lever for re-running per-file
# metadata extraction that doesn't depend on chunk boundaries at
# all (symbol_index extraction, import_edges extraction, and now
# call_edges extraction — see "Call & Import Graph" in
# docs/architecture-deep-dive.md) for files whose content hasn't changed:
# index_file()'s unchanged-content fast path is gated on this same
# version, not a separate one. Bumped to 3 (2026-08) to force a
# one-time backfill of import_edges, then to 4 (2026-08) to force a
# one-time backfill of call_edges — not because chunk boundaries
# themselves changed either time. Accept the one-time re-embedding
# cost this causes; it's the established pattern, not a new one
# (symbol_index adoption relied on the same lever). Bumped to 5
# (2026-08) for the tree-sitter chunk boundaries + Angular decorator
# fix (see "Structure-aware chunking" in docs/architecture-deep-dive.md) —
# .cs/.java/.ts/.tsx boundaries can now differ from the previous regex-only pass.
# Bumped again to 6 (2026-08) because an earlier ad-hoc `docker cp` of
# a mid-development chunker.py into the *running* watcher container
# (done for fast fixture-testing iteration, before the final image
# rebuild) caused some files to be re-chunked and stamped with
# chunker_version=5 under buggy pre-fix code (missing the
# _CONTAINER_ONLY_NODE_TYPES namespace fix). The subsequent proper
# rebuild's reconciliation pass then skipped re-chunking those files
# (content_sha256 + chunker_version both already matched), silently
# carrying forward incorrect/overlapping chunk boundaries. This bump
# forces a clean, fully-correct re-chunk of every file.
#
# Bumped again to 7 (2026-08): companion fix to upsert_chunks_delta() in
# db.py, which previously skipped refreshing Qdrant payloads for
# text-unchanged chunks even when their chunk_type/start_line/end_line
# classification changed under the v5/v6 tree-sitter rewrite
# (stable_chunk_id hashes chunk TEXT only). db.py now also calls
# set_payload() for those points, but that only runs on the NEXT
# re-chunk of each file — this version bump is what forces that
# one-time re-chunk repo-wide so every already-indexed file's stale
# payload (wrong chunk_type/boundaries left over from the v5/v6
# rollout) gets refreshed once, not just newly-changed files.

# ── Kotlin ────────────────────────────────────────────────────────────────────
_KT_TOP = re.compile(
    r"^(?:(?:public|private|protected|internal|abstract|open|data|sealed|inline|suspend|override|companion)\s+)*"
    r"(?:class|object|interface|fun|typealias)\s+(\w+)",
    re.MULTILINE,
)

# ── TypeScript / JavaScript ───────────────────────────────────────────────────
_TS_TOP = re.compile(
    r"^(?:export\s+)?(?:default\s+)?"
    r"(?:async\s+)?(?:abstract\s+)?"
    r"(?:class|function|const|interface|type|enum)\s+(\w+)",
    re.MULTILINE,
)
_TS_ARROW = re.compile(
    r"^(?:export\s+)?const\s+(\w+)\s*[:=].*(?:=>|\bfunction\b)",
    re.MULTILINE,
)

# Angular decorator: used to tag chunks whose leading line is a decorator
_NG_DECORATOR_RE = re.compile(r"^\s*@(?:Component|Injectable|Directive|Pipe|NgModule)")

# ── Python ────────────────────────────────────────────────────────────────────
_PY_TOP = re.compile(
    r"^(?:async\s+)?(?:class|def)\s+(\w+)",
    re.MULTILINE,
)

# ── C# ────────────────────────────────────────────────────────────────────────
# Matches top-level type declarations with any combination of C# access/modifier keywords.
# Methods are always indented inside a type body, so ^ anchoring excludes them.
# The capture group uses [\w.]+ to grab dotted namespace names (e.g. "MyApp.Services").
_CS_TOP = re.compile(
    r"^(?:(?:public|private|protected|internal|abstract|sealed|static|partial|override|virtual|async|new|readonly|unsafe)\s+)*"
    r"(?:class|interface|enum|struct|record|namespace|delegate)\s+([\w.]+)",
    re.MULTILINE,
)

# ── Java ──────────────────────────────────────────────────────────────────────
# Handles classes, interfaces, enums, records (Java 14+), and annotation types (@interface).
_JAVA_TOP = re.compile(
    r"^(?:(?:public|private|protected|abstract|final|static|sealed|non-sealed|strictfp)\s+)*"
    r"(?:@interface|class|interface|enum|record)\s+(\w+)",
    re.MULTILINE,
)

# ── Swift ─────────────────────────────────────────────────────────────────────
# Top-level declarations only (^ anchor). "class func" is always indented inside a
# type body in well-formed Swift, so it won't be captured here.
_SWIFT_TOP = re.compile(
    r"^(?:(?:public|private|internal|fileprivate|open|final|override|static|@objc|@objcMembers|@main)\s+)*"
    r"(?:class|struct|enum|protocol|func|extension|typealias|actor)\s+(\w+)",
    re.MULTILINE,
)

# ── Go ────────────────────────────────────────────────────────────────────────
# Covers func declarations (both plain and methods with a receiver, e.g.
# "func (r *Repo) Save(...)") and named type declarations ("type Foo struct{...}",
# "type Bar interface{...}", or a plain type alias). Single capture group: the
# receiver clause (if any) is consumed non-capturing so the group always lands
# on the actual function/type name.
_GO_TOP = re.compile(
    r"^(?:func\s+(?:\([^)]*\)\s*)?|type\s+)(\w+)",
    re.MULTILINE,
)

# ── Rust ──────────────────────────────────────────────────────────────────────
# fn/struct/enum/trait/impl at column 0 (top-level or impl-block-level in a
# typical rustfmt-formatted file; methods nested inside "impl { ... }" are
# indented and won't match here, same "only true top-level" convention as
# C#/Java above). Generic parameters (`<T>`) after the keyword are skipped
# non-capturing so the group lands on the bare name.
_RUST_TOP = re.compile(
    r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?"
    r"(?:fn|struct|enum|trait|impl)\s+(?:<[^>]*>\s*)?(\w+)",
    re.MULTILINE,
)

# ── Ruby ──────────────────────────────────────────────────────────────────────
# class/module/def at column 0. Supports namespaced constants (Foo::Bar).
# Methods nested inside a class/module body are indented and won't match here
# (same convention as the other languages above) — only top-level
# class/module declarations and standalone top-level `def`s (common in
# procedural scripts/Rakefiles) are captured.
_RUBY_TOP = re.compile(
    r"^(?:class|module|def)\s+(\w+(?:::\w+)*)",
    re.MULTILINE,
)

# ── PHP ───────────────────────────────────────────────────────────────────────
_PHP_TOP = re.compile(
    r"^(?:abstract\s+|final\s+)*(?:class|interface|trait|function)\s+&?(\w+)",
    re.MULTILINE,
)

# ── C / C++ ───────────────────────────────────────────────────────────────────
# Two separate patterns, merged by _chunk_by_pattern (see its docstring):
# aggregate-type declarations (struct/class/enum/union) and function
# *definitions* have very different shapes in C/C++, unlike the
# keyword-prefixed-declaration languages above.
#
# Known heuristic limitations (same "good enough, not a full parser" bar as
# every other pattern in this file, but worth being explicit about for C/C++
# specifically since its syntax is the least regular here):
#   - Only matches a definition if its opening "{" is on the same source line
#     as the signature — a very common but not universal C/C++ style; a
#     signature whose "{" starts on the next line won't be caught.
#   - Anonymous aggregates named via a trailing typedef alias
#     ("typedef struct { ... } Foo;") aren't matched — only
#     "struct Foo { ... }" / "typedef struct Foo { ... }" shapes are.
#   - Nested parens inside parameter types (e.g. a function-pointer
#     parameter) aren't handled; such signatures may fail to match or match
#     with a truncated parameter list captured as part of the boundary text
#     (harmless — chunk boundaries are still reasonable, just imprecise).
_C_AGGREGATE = re.compile(
    r"^(?:typedef[ \t]+)?(?:struct|class|enum(?:[ \t]+class)?|union)\s+(\w+)",
    re.MULTILINE,
)
# Excludes control-flow keywords from being mistaken for a function name when
# a return-type prefix is absent or minimal (e.g. "if (x > 0) {" must NOT be
# treated as a function named "if").
_C_CONTROL_KEYWORDS = r"if|for|while|switch|catch|else|return|do|sizeof|defined"
# NOTE: uses [ \t] (not \s) for intra-line whitespace throughout — \s also
# matches newlines, which would let the lazy "return type" prefix below start
# matching from an earlier blank (or otherwise unrelated) line and swallow
# forward into the real signature, corrupting which line gets reported as
# this match's start (found via testing: a function immediately preceded by
# a blank line was reported as starting on that blank line instead of its
# real signature line, which then fed the wrong line into _detect_chunk_type
# and mis-classified it). Confining to space/tab keeps every match's `^`
# anchor on the line the signature actually starts on.
_C_FUNCTION = re.compile(
    r"^(?:static[ \t]+|inline[ \t]+|extern[ \t]+|virtual[ \t]+|constexpr[ \t]+|explicit[ \t]+)*"
    r"[\w:<>,\*&\t ]+?\b(?!(?:" + _C_CONTROL_KEYWORDS + r")\b)(\w+)[ \t]*"
                                                        r"\([^;{)]*\)[ \t]*(?:const[ \t]*)?\{",
    re.MULTILINE,
)

# ── Markdown headings ─────────────────────────────────────────────────────────
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)", re.MULTILINE)

# ── Vue SFC top-level blocks ──────────────────────────────────────────────────
# Matches opening tags of SFC top-level blocks: <template>, <script>, <style>
# (with any attributes, e.g. <script setup lang="ts">)
_VUE_BLOCK = re.compile(r"^(<(?:template|script|style)(?:\s[^>]*)?>)", re.MULTILINE | re.IGNORECASE)

# ── YAML / TOML top-level keys ────────────────────────────────────────────────
# Matches non-indented lines that look like "key:" (YAML) or "key =" (TOML),
# not blank and not comments.
_YAML_KEY = re.compile(r"^(?![ \t#])(\S[^:#\n]*)\s*[=:]", re.MULTILINE)

# ── SQL statement boundary ────────────────────────────────────────────────────
_SQL_STMT_END = re.compile(r";\s*$", re.MULTILINE)

# Extracts a name from a CREATE statement (TABLE/VIEW/INDEX/FUNCTION/PROCEDURE/
# TRIGGER), tolerating "IF NOT EXISTS", schema-qualified names, and optional
# quoting (backticks, double quotes, or square brackets). Used to populate
# chunk "symbol" for SQL chunks — previously always None (see indexer.py's
# symbol_index fallback, which relies on chunker-derived symbol/chunk_type
# for languages tree-sitter doesn't cover).
_SQL_CREATE_NAME = re.compile(
    r"\bcreate\s+(?:or\s+replace\s+)?"
    r"(table|view|index|unique\s+index|function|procedure|trigger)\s+"
    r"(?:if\s+not\s+exists\s+)?"
    r"[\"'`\[]?([\w.]+)[\"'`\]]?",
    re.IGNORECASE,
)
# Strips "-- ..." line comments before running _SQL_CREATE_NAME against a
# chunk's text — without this, prose in a comment that happens to mention
# "CREATE TABLE ..." (e.g. a header comment like "-- All tables use CREATE
# TABLE IF NOT EXISTS.") can match before the real statement below it, since
# .search() takes the first match in the whole chunk.
_SQL_LINE_COMMENT = re.compile(r"--.*$", re.MULTILINE)


# ── Chunk type classifier ─────────────────────────────────────────────────────

def _detect_chunk_type(line: str) -> str:
    """Classify a top-level declaration line into a chunk_type.

    Language-agnostic: looks for structural keywords that are shared across
    Kotlin, TypeScript, JavaScript, Python, C#, Java, and Swift rather than
    per-language modifier lists (which differed and missed multi-modifier lines).
    """
    stripped = line.lstrip()

    # Angular / TS decorators (checked first — line may start with @)
    if _NG_DECORATOR_RE.match(stripped):
        return "component"

    # Function-like declarations across all supported languages:
    #   TS/JS: function foo(), async function foo()
    #   Kotlin: fun foo(), suspend fun foo()
    #   Python: def foo(), async def foo()
    #   Swift:  func foo()
    #   Go:     func foo(), func (r *Receiver) foo()
    #   Rust:   fn foo()
    #   Ruby:   def foo()  (same "def " token as Python)
    #   PHP:    function foo()  (same "function" token as TS/JS)
    #   (C# / Java methods are always indented → won't reach here)
    #
    # Note: the trailing \s (not baked into each alternative as a literal
    # space, e.g. "func ") is deliberate — Go's receiver-method syntax
    # ("func (r *Repo) Save(...)") has "func" followed by "(", not a word
    # character, so a \b requiring a word char immediately after "func "
    # would never match it (space→"(" isn't a word-boundary transition).
    # Requiring \s as a separate token after the keyword's own \b sidesteps
    # that: the boundary only has to hold around the keyword itself.
    if re.search(r"\b(?:function|fun|def|func|fn)\b\s", stripped):
        return "function"

    # Arrow-function / const-fn expressions (TS/JS)
    if re.match(r"(?:export\s+)?const\s+\w+\s*[:=].*(?:=>|\bfunction\b)", stripped):
        return "function"

    # Class-like type declarations across all languages:
    #   class, interface, struct, enum, record, protocol, actor    (universal)
    #   typealias, object, extension, namespace, delegate          (language-specific)
    #   trait, impl                                                (Rust, also PHP's "trait")
    #   module                                                     (Ruby)
    #   union                                                      (C/C++)
    #   @interface                                                  (Java annotation type)
    if re.search(
            r"\b(?:class|interface|struct|enum|record|protocol|actor|typealias|object|extension|namespace|delegate|trait|impl|module|union)\b"
            r"|@interface\b",
            stripped,
    ):
        return "class"

    # C/C++ function *definitions* have no distinguishing keyword (unlike
    # every other language above, all of which have one) — classify by
    # shape instead: a line ending in a parameter list + opening brace, once
    # nothing keyword-based matched above. Excludes common control-flow
    # constructs that share the same "(...) {" shape.
    if (
            re.search(r"\)\s*(?:const\s*)?\{\s*$", stripped)
            and not re.match(rf"(?:{_C_CONTROL_KEYWORDS})\b", stripped)
    ):
        return "function"

    return "block"


# ── Pattern-based chunker (used by all AST-boundary languages) ────────────────

def _chunk_by_pattern(lines: list[str], pattern) -> list[dict]:
    """
    pattern: a single compiled regex (exactly one capture group for the
    symbol name), or a list/tuple of such regexes. When given a list, matches
    from every pattern are merged and sorted by position — needed for
    languages where one regex can't cleanly express all boundary shapes
    (e.g. C/C++'s struct/class/enum declarations vs. function *definitions*
    look nothing alike; see _C_AGGREGATE / _C_FUNCTION below).
    """
    patterns = pattern if isinstance(pattern, (list, tuple)) else [pattern]
    source = "\n".join(lines)
    matches = []
    for p in patterns:
        matches.extend(p.finditer(source))
    if not matches:
        return _chunk_by_paragraphs(lines)
    matches.sort(key=lambda m: m.start())

    starts = []  # (line_index, symbol_name)
    for m in matches:
        line_idx = source[: m.start()].count("\n")
        starts.append((line_idx, m.group(1) if m.lastindex else None))

    chunks = []
    # Preamble chunk (package/imports/annotations before the first match) —
    # mirrors the same pattern already used by _chunk_vue/_chunk_markdown/_chunk_yaml.
    if starts and starts[0][0] > 0:
        preamble_text = "\n".join(lines[0:starts[0][0]]).strip()
        if preamble_text:
            chunks.append({
                "text": preamble_text,
                "start_line": 1,
                "end_line": starts[0][0],
                "symbol": None,
                "chunk_type": "block",
            })
    for i, (start_line, symbol) in enumerate(starts):
        end_line = starts[i + 1][0] if i + 1 < len(starts) else len(lines)
        text = "\n".join(lines[start_line:end_line]).strip()
        if text:
            chunks.append({
                "text": text,
                "start_line": start_line + 1,
                "end_line": end_line,
                "symbol": symbol,
                "chunk_type": _detect_chunk_type(lines[start_line]),
            })
    return chunks  # preamble chunk (if any) is always first in this list


# ── Tree-sitter-driven chunker (Tier‑1, .cs/.java/.ts/.tsx) ───────────────────
# See "Structure-aware chunking" in docs/architecture-deep-dive.md. Reuses
# the exact same parsers
# and node-type tables symbol_extractor.py already has installed/configured,
# so chunk boundaries and symbol_index rows come from the SAME parse tree
# instead of two independently-drifting mechanisms (regex vs. tree-sitter)
# that can desync on generics, decorators/annotations, and multi-line
# signatures. Additive only: chunk_file()'s dispatch falls back to
# _chunk_by_pattern() whenever this returns None (tree-sitter unavailable,
# parse error, or zero top-level declarations found).

# Wrapper node types that are pure organizational containers, not
# independently meaningful chunk units — recursed through rather than
# emitted as a boundary, so e.g. a C# namespace doesn't collapse every class
# and interface nested inside it into one oversized chunk (that would defeat
# the whole point of tree-sitter boundaries for exactly the language this
# rollout targets first — see plan §2.3's rollout order). Namespaces still
# get their own symbol_index row via symbol_extractor.py; this only affects
# chunk *boundaries*.
_CONTAINER_ONLY_NODE_TYPES = {"namespace_declaration"}


def _collect_top_level(node, node_types: set[str]) -> list[tuple[int, str | None, str]]:
    """
    Collect (start_line, name, node_type) triples for declaration nodes,
    using the parse tree instead of indentation to find them. Deliberately
    does NOT descend into a matched node's own children — a class's methods
    stay inside its single chunk exactly like today's regex chunker (whose
    `^`-anchored patterns never match indented methods either); this is a
    chunk-*boundary* swap-in, not a request to chunk at every nested node
    (see this file's module docstring and plan §2.1's explicit non-goal).
    Pure container types (_CONTAINER_ONLY_NODE_TYPES) are the one exception:
    always recursed through, never emitted as a boundary themselves.
    """
    results: list[tuple[int, str | None, str]] = []

    def _visit(n) -> None:
        if n.type in node_types and n.type not in _CONTAINER_ONLY_NODE_TYPES:
            name_node = n.child_by_field_name("name")
            name = (
                name_node.text.decode("utf-8", errors="replace")
                if name_node is not None and name_node.text else None
            )
            results.append((n.start_point[0] + 1, name, n.type))
            return  # do not descend further — see docstring above
        for child in n.children:
            _visit(child)

    _visit(node)
    results.sort(key=lambda t: t[0])
    return results


def _chunk_by_treesitter(path: str, lines: list[str]) -> list[dict] | None:
    """
    Tree-sitter-driven chunk boundaries (plan §2.1). Returns None — triggering
    the _chunk_by_pattern() fallback in chunk_file() — when tree-sitter is
    unavailable, the file fails to parse, or zero top-level declaration nodes
    are found. Same return shape as _chunk_by_pattern(): list[dict] of
    {text, start_line, end_line, symbol, chunk_type}.
    """
    if not symbol_extractor._TS_AVAILABLE:
        return None

    ext = Path(path).suffix.lower()
    lang_name = symbol_extractor._EXT_TO_LANG.get(ext)
    if not lang_name:
        return None
    node_types = symbol_extractor._SYMBOL_NODE_TYPES.get(lang_name)
    if not node_types:
        return None

    try:
        parser = symbol_extractor.get_parser(lang_name)
        tree = parser.parse("\n".join(lines).encode("utf-8", errors="replace"))
    except Exception:
        return None

    starts = _collect_top_level(tree.root_node, node_types)
    if not starts:
        return None

    chunks: list[dict] = []
    # Preamble chunk (package/imports/annotations before the first top-level
    # declaration) — same convention as _chunk_by_pattern's preamble handling.
    if starts[0][0] > 1:
        preamble_text = "\n".join(lines[0:starts[0][0] - 1]).strip()
        if preamble_text:
            chunks.append({
                "text": preamble_text,
                "start_line": 1,
                "end_line": starts[0][0] - 1,
                "symbol": None,
                "chunk_type": "block",
            })
    for i, (start_line, name, node_type) in enumerate(starts):
        end_line = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(lines)
        text = "\n".join(lines[start_line - 1:end_line]).strip()
        if text:
            chunks.append({
                "text": text,
                "start_line": start_line,
                "end_line": end_line,
                "symbol": name,
                # Tree-sitter node type is strictly more reliable than
                # re-deriving the same information via _detect_chunk_type's
                # regex-based line classifier — reuse symbol_extractor's map.
                "chunk_type": symbol_extractor._KIND_MAP.get(node_type, "block"),
            })
    return chunks  # preamble chunk (if any) is always first in this list


# ── Angular decorator-attachment fix (.ts/.tsx only) ──────────────────────────
# plan §2.2 — a class/function chunk immediately preceded by an Angular
# decorator (@Component/@Injectable/@Directive/@Pipe/@NgModule) should carry
# the decorator with it, not orphan it in the previous chunk or the preamble.
# Applied as a post-pass over the already-produced chunk list (from either
# _chunk_by_pattern or _chunk_by_treesitter — both code paths need it, since
# the regex fallback can still be hit per-file even once tree-sitter chunking
# is live for .ts/.tsx).

def _find_decorator_start(declaration_start_line: int, lines: list[str]) -> int | None:
    """
    Given a class/function declaration's current 1-based start_line, look
    backward for an immediately-preceding Angular decorator line, tolerating
    exactly one blank line in between and a multi-line decorator argument
    list (e.g. a multi-line `@Component({ selector: ..., template: ... })`).
    Returns the decorator's own 1-based start line, or None if there isn't one.
    """
    idx = declaration_start_line - 2  # 0-based index of the line just above
    if idx >= 0 and not lines[idx].strip():
        idx -= 1  # allow exactly one blank line between decorator and declaration
    if idx < 0:
        return None

    candidate = lines[idx].strip()
    if _NG_DECORATOR_RE.match(candidate):
        return idx + 1  # single-line decorator, e.g. "@Injectable()"

    if not candidate.endswith((")", "}", "})", "]")):
        return None  # not the tail of a bracketed decorator call

    # Walk backward, depth-counting brackets (same technique as
    # _dispatch_block_span's forward brace-matching), to find the decorator
    # call's own opening line.
    depth = 0
    j = idx
    while j >= 0:
        for ch in reversed(lines[j]):
            if ch in ")}]":
                depth += 1
            elif ch in "({[":
                depth -= 1
                if depth == 0:
                    return j + 1 if _NG_DECORATOR_RE.match(lines[j].strip()) else None
        j -= 1
    return None  # unbalanced brackets — bail out rather than overshoot


def _apply_decorator_fix(chunks: list[dict], lines: list[str], ext: str) -> list[dict]:
    """
    Extend class/function chunks backward across a preceding Angular
    decorator, shrinking the previous chunk's end_line to match so the two
    chunks stay non-overlapping (dropping the previous chunk entirely if it
    becomes empty — e.g. it was only the decorator, nothing else).
    """
    if ext not in (".ts", ".tsx"):
        return chunks

    for i, chunk in enumerate(chunks):
        if chunk["chunk_type"] not in ("class", "function"):
            continue
        decorator_start = _find_decorator_start(chunk["start_line"], lines)
        if decorator_start is None or decorator_start >= chunk["start_line"]:
            continue

        if i > 0:
            prev = chunks[i - 1]
            if prev["end_line"] >= decorator_start:
                prev["end_line"] = decorator_start - 1
                prev["text"] = "\n".join(lines[prev["start_line"] - 1:prev["end_line"]]).strip()

        chunk["start_line"] = decorator_start
        chunk["text"] = "\n".join(lines[decorator_start - 1:chunk["end_line"]]).strip()

    return [c for c in chunks if c["text"]]


# ── Dispatch-table / switch-case sub-chunking ─────────────────────────────────
# A switch/when/match dispatch block is itself a string/pattern-keyed dispatch
# table — the same code shape chunker.py's own chunk_file() dispatch is (see
# its own docstring). Neither identifier lookup (symbol_index) nor semantic
# search can anchor on a value computed inside one arm, because an arm has no
# declaration-like boundary line of its own — it's just a label inside a
# function/class's existing single chunk. This sub-chunks a dispatch block's
# arms into their own chunks (chunk_type="case") *within* an already-produced
# top-level chunk, so an arm becomes independently searchable/identifiable —
# it does not compete with or replace the top-level function/class chunking
# above; it runs as an additional pass over each of those chunks' bodies.
#
# The *shape* (a dispatch keyword, then N arm-labels, each starting a
# variable-length body) is common across languages; only the keywords differ.
# One shared helper below, driven by a small per-language (dispatch, arm)
# regex table, exactly mirroring the per-extension pattern table above.
_SWITCH_START = re.compile(r"\bswitch\s*\(")
_SWITCH_ARM = re.compile(r"^\s*case\s+(.+?):", re.MULTILINE)
_WHEN_START = re.compile(r"\bwhen\s*[({]")
_WHEN_ARM = re.compile(r"^\s*(.+?)\s*->", re.MULTILINE)
_MATCH_START = re.compile(r"\bmatch\s+.+:")
_MATCH_ARM = re.compile(r"^\s*case\s+(.+?):", re.MULTILINE)

# Extension → (dispatch_start_re, arm_start_re). Extensions not listed here
# get no dispatch sub-chunking (e.g. Go/Rust/Ruby/PHP/C — out of scope for
# this pass; their own top-level chunking from _GO_TOP etc. is unaffected).
_DISPATCH_TABLE: dict[str, tuple[re.Pattern, re.Pattern]] = {
    ".ts": (_SWITCH_START, _SWITCH_ARM), ".tsx": (_SWITCH_START, _SWITCH_ARM),
    ".js": (_SWITCH_START, _SWITCH_ARM), ".jsx": (_SWITCH_START, _SWITCH_ARM),
    ".java": (_SWITCH_START, _SWITCH_ARM),
    ".cs": (_SWITCH_START, _SWITCH_ARM),
    ".swift": (_SWITCH_START, _SWITCH_ARM),
    ".kt": (_WHEN_START, _WHEN_ARM), ".kts": (_WHEN_START, _WHEN_ARM),
    ".py": (_MATCH_START, _MATCH_ARM),
}

_MIN_ARMS_FOR_SUBCHUNK = 3  # heuristic gate — see module-level note above


def _dispatch_block_span(body_text: str, dispatch_match: re.Match) -> tuple[int, int] | None:
    """
    Find the {...} extent of a brace-delimited dispatch block (switch/when),
    searching for the first '{' starting just after the dispatch keyword's
    own match (e.g. after "switch (x.getType())"). Returns (start, end) as
    character offsets bounding the block's *inner* content (start is just
    after the opening '{', end is at the matching '}', found via simple
    depth-counting — no string/comment awareness, same heuristic bar as
    everything else in this file). Returns None if no '{' follows at all —
    e.g. Python's `match x:` has no braces (indentation-based instead), so
    arm-matching there falls back to the whole chunk body (harmless: Python's
    arm keyword is `case`, which doesn't collide with unrelated syntax the
    way Kotlin/TS's `->` does with lambda expressions — see the bug this
    scoping fixes, noted where _chunk_dispatch_arms calls this).
    """
    open_pos = body_text.find("{", dispatch_match.end())
    if open_pos == -1:
        return None
    depth = 0
    for i in range(open_pos, len(body_text)):
        if body_text[i] == "{":
            depth += 1
        elif body_text[i] == "}":
            depth -= 1
            if depth == 0:
                return (open_pos + 1, i)
    return (open_pos + 1, len(body_text))  # unbalanced braces — scope to end


def _chunk_dispatch_arms(
        lines: list[str],
        chunk: dict,
        dispatch_start_re: re.Pattern,
        arm_start_re: re.Pattern,
) -> list[dict]:
    """
    Given one already-produced top-level chunk (a function/class chunk from
    _chunk_by_pattern above), check whether its body contains a dispatch
    block (switch/when/match) with >= _MIN_ARMS_FOR_SUBCHUNK arms. If so,
    split it into a "head" chunk (everything from the chunk's own start up
    to the first arm — keeps the enclosing declaration + dispatch header
    together) plus one chunk per arm (chunk_type="case", symbol = the arm's
    label text). Returns [chunk] unchanged if no qualifying dispatch block
    is found — this must be a no-op for ordinary chunks.

    Arm-matching is scoped to the dispatch block's own {...} body (via
    _dispatch_block_span) when one can be found, NOT the whole chunk —
    found via testing that this matters, not just a theoretical concern:
    Kotlin's `when` arm regex (`-> `) also matches ordinary lambda
    expressions elsewhere in the same function (e.g.
    `list.map { it -> it * 2 }`), which would otherwise be mistaken for
    dispatch arms and corrupt the split. Braces aren't available for
    Python's `match` (indentation-based) — falls back to whole-chunk-body
    search there, which is safe since `case` doesn't collide with anything.

    Known remaining limitation: a chunk containing more than one separate
    top-level switch/when block would still only have the *first* one
    scoped correctly (uncommon in practice — most functions have at most
    one dispatch block).
    """
    start0 = chunk["start_line"] - 1  # 0-based, inclusive
    end0 = chunk["end_line"]  # 0-based, exclusive
    body_text = "\n".join(lines[start0:end0])

    dispatch_match = dispatch_start_re.search(body_text)
    if not dispatch_match:
        return [chunk]

    span = _dispatch_block_span(body_text, dispatch_match)
    search_text, search_offset = (body_text[span[0]:span[1]], span[0]) if span else (body_text, 0)

    arm_matches = list(arm_start_re.finditer(search_text))
    if len(arm_matches) < _MIN_ARMS_FOR_SUBCHUNK:
        return [chunk]  # not "dispatch-table style" enough to bother

    arm_starts = []  # (absolute 0-based line index, label)
    for m in arm_matches:
        abs_char_pos = search_offset + m.start()
        rel_line = body_text[:abs_char_pos].count("\n")
        label = m.group(1).strip() if m.lastindex else None
        if label:
            label = label.strip("\"'")  # `case "get_index_status":` -> get_index_status
        arm_starts.append((start0 + rel_line, label))

    result = []

    head_end = arm_starts[0][0]
    head_text = "\n".join(lines[start0:head_end]).strip()
    if head_text:
        result.append({
            "text": head_text,
            "start_line": start0 + 1,
            "end_line": head_end,
            "symbol": chunk["symbol"],
            "chunk_type": chunk["chunk_type"],
        })

    for i, (arm_start, label) in enumerate(arm_starts):
        arm_end = arm_starts[i + 1][0] if i + 1 < len(arm_starts) else end0
        arm_text = "\n".join(lines[arm_start:arm_end]).strip()
        if arm_text:
            result.append({
                "text": arm_text,
                "start_line": arm_start + 1,
                "end_line": arm_end,
                "symbol": label,
                "chunk_type": "case",
            })

    return result or [chunk]


def _apply_dispatch_subchunking(chunks: list[dict], lines: list[str], ext: str) -> list[dict]:
    """Runs _chunk_dispatch_arms over every chunk, for extensions with a
    dispatch-table entry (see _DISPATCH_TABLE) — a no-op for other extensions."""
    pair = _DISPATCH_TABLE.get(ext)
    if not pair:
        return chunks
    dispatch_re, arm_re = pair
    result = []
    for c in chunks:
        result.extend(_chunk_dispatch_arms(lines, c, dispatch_re, arm_re))
    return result


def _chunk_by_paragraphs(lines: list[str], max_lines: int = 80) -> list[dict]:
    """Fallback: split on blank lines, respect max_lines per chunk."""
    chunks = []
    buf: list[str] = []
    buf_start = 0

    def flush():
        if buf:
            text = "\n".join(buf).strip()
            if text:
                chunks.append({
                    "text": text,
                    "start_line": buf_start + 1,
                    "end_line": buf_start + len(buf),
                    "symbol": None,
                    "chunk_type": "block",
                })

    for i, line in enumerate(lines):
        if not line.strip():
            if buf:
                flush()
                buf = []
                buf_start = i + 1
        else:
            if not buf:
                buf_start = i
            buf.append(line)
            if len(buf) >= max_lines:
                flush()
                buf = []
                buf_start = i + 1

    flush()
    return chunks


# ── Tree-index gate (for Markdown files) ─────────────────────────────────────

def should_use_tree_index(file_path: str, text: str, config: dict) -> bool:
    """Return True if this .md file should be tree-indexed instead of Qdrant-chunked."""
    doc_cfg = config.get("doc_index", {})
    if not doc_cfg.get("enabled", False):
        return False
    # Always-tree paths (relative to project root)
    always_paths = doc_cfg.get("always_tree_paths", [])
    # Normalise file_path: strip leading separators for comparison
    norm_path = file_path.lstrip("/\\")
    if any(norm_path.startswith(p.lstrip("/\\")) for p in always_paths):
        return True
    # Heading-count threshold
    min_h = doc_cfg.get("min_headings_for_tree", 8)
    heading_count = sum(1 for line in text.splitlines() if line.startswith("#"))
    return heading_count >= min_h


# ── Token-limit guard ─────────────────────────────────────────────────────────

def _split_chunk_to_max(chunk: dict, max_chars: int) -> list[dict]:
    """Split a single chunk that exceeds *max_chars* into smaller sub-chunks.

    Splitting is done at line boundaries so that each sub-chunk stays under the
    character limit.  Only the first sub-chunk keeps the original symbol name;
    subsequent sub-chunks set symbol=None to avoid duplicating the declaration
    label in the vector store.
    """
    text = chunk["text"]
    if len(text) <= max_chars:
        return [chunk]

    lines = text.splitlines()
    result: list[dict] = []
    buf: list[str] = []
    buf_chars = 0
    sub_start = chunk["start_line"]  # 1-based

    for line in lines:
        line_chars = len(line) + 1  # +1 for the newline
        if buf and buf_chars + line_chars > max_chars:
            sub_text = "\n".join(buf).strip()
            if sub_text:
                result.append({
                    "text": sub_text,
                    "start_line": sub_start,
                    "end_line": sub_start + len(buf) - 1,
                    "symbol": chunk["symbol"] if not result else None,
                    "chunk_type": chunk["chunk_type"],
                })
            sub_start += len(buf)
            buf = []
            buf_chars = 0
        buf.append(line)
        buf_chars += line_chars

    # Flush the remaining buffer
    if buf:
        sub_text = "\n".join(buf).strip()
        if sub_text:
            result.append({
                "text": sub_text,
                "start_line": sub_start,
                "end_line": sub_start + len(buf) - 1,
                "symbol": chunk["symbol"] if not result else None,
                "chunk_type": chunk["chunk_type"],
            })

    return result if result else [chunk]


def _apply_max_chunk_size(chunks: list[dict], max_chars: int) -> list[dict]:
    """Apply the character cap to every chunk in *chunks*, splitting oversized ones."""
    if max_chars <= 0:
        return chunks
    result: list[dict] = []
    for chunk in chunks:
        result.extend(_split_chunk_to_max(chunk, max_chars))
    return result


# ── Main dispatch ────────────────────────────────────────────────────────────

def chunk_file(path: str, content: str, config: dict | None = None) -> tuple[list[dict], bool]:
    """Main entry point.  Returns (list[chunk_dicts], use_tree_index).

    When use_tree_index is True: the caller must NOT store chunks in Qdrant —
    the file will be indexed as a doc tree instead.

    Dispatches by file extension using Python's match/case (3.10+).
    Each arm picks the most meaningful boundary strategy for that file type;
    all fall back to the paragraph chunker when no structural boundaries are found.

    After chunking, every chunk is checked against the embedding model's token-context
    limit (configured via indexer.max_chunk_chars, default 6 000 characters ≈ 1 500
    tokens for nomic-embed-text-v1.5's 2 048-token window).  Oversized chunks are
    automatically split at line boundaries.

    *config* must be the full merged project config (has "indexer" and "doc_index"
    as top-level keys).  Passing only the doc_index subsection is a legacy mistake
    that silently disables tree-indexing — always pass cfg.get_project().
    """
    if config is None:
        config = {}
    ext = Path(path).suffix.lower()
    lines = content.splitlines()

    # Maximum characters per chunk — avoids truncation warnings from the
    # embedding model (nomic-embed-text-v1.5 context limit = 2 048 tokens).
    max_chars: int = int(config.get("indexer", {}).get("max_chunk_chars", 6000))

    # ── Tree-index gate (honours doc_index.tree_extensions) ──────────────────
    # Any extension listed in tree_extensions can trigger PageIndex-style tree
    # indexing.  The decision is made here — before the language dispatch —
    # so callers don't need to know which extensions are tree-eligible.
    tree_exts = set(config.get("doc_index", {}).get("tree_extensions", [".md"]))
    if ext in tree_exts and should_use_tree_index(path, content, config):
        return [], True

    match ext:
        # ── Compiled / JVM ───────────────────────────────────────────────────
        case ".kt" | ".kts":
            chunks = _chunk_by_pattern(lines, _KT_TOP)

        case ".java":
            chunks = _chunk_by_treesitter(path, lines) or _chunk_by_pattern(lines, _JAVA_TOP)

        case ".cs":
            chunks = _chunk_by_treesitter(path, lines) or _chunk_by_pattern(lines, _CS_TOP)

        # ── Web / scripting ──────────────────────────────────────────────────
        case ".ts" | ".tsx":
            chunks = _chunk_by_treesitter(path, lines) or _chunk_by_pattern(lines, _TS_TOP)
            # Standalone arrow-function components not caught by _TS_TOP
            if not chunks:
                chunks = _chunk_by_pattern(lines, _TS_ARROW)
            chunks = _apply_decorator_fix(chunks, lines, ext)

        case ".js" | ".jsx":
            chunks = _chunk_by_pattern(lines, _TS_TOP)

        case ".vue":
            chunks = _chunk_vue(lines)

        case ".py":
            chunks = _chunk_by_pattern(lines, _PY_TOP)

        # ── Apple platforms ──────────────────────────────────────────────────
        case ".swift":
            chunks = _chunk_by_pattern(lines, _SWIFT_TOP)

        # ── Systems / other compiled languages ──────────────────────────────
        case ".go":
            chunks = _chunk_by_pattern(lines, _GO_TOP)

        case ".rs":
            chunks = _chunk_by_pattern(lines, _RUST_TOP)

        case ".rb":
            chunks = _chunk_by_pattern(lines, _RUBY_TOP)

        case ".php":
            chunks = _chunk_by_pattern(lines, _PHP_TOP)

        case ".c" | ".cc" | ".cpp" | ".cxx" | ".h" | ".hpp":
            chunks = _chunk_by_pattern(lines, [_C_AGGREGATE, _C_FUNCTION])

        # ── Prose / docs ─────────────────────────────────────────────────────
        case ".md":
            # Pre-check: large docs → tree index (no Qdrant chunks)
            if should_use_tree_index(path, content, config):
                return [], True
            # Small docs → existing heading-boundary chunking into Qdrant
            chunks = _chunk_markdown(lines)

        # ── Data / config ────────────────────────────────────────────────────
        case ".sql":
            chunks = _chunk_sql(content, lines)

        case ".yaml" | ".yml" | ".toml":
            chunks = _chunk_yaml(lines)

        # ── Everything else (HTML, CSS, JSON, shell, Dockerfile, …) ─────────
        case _:
            chunks = _chunk_by_paragraphs(lines)

    # Additional pass: sub-chunk switch/when/match dispatch arms within
    # already-produced chunks (no-op for extensions not in _DISPATCH_TABLE).
    chunks = _apply_dispatch_subchunking(chunks, lines, ext)

    return _apply_max_chunk_size(chunks, max_chars), False


# ── Vue SFC block chunker ─────────────────────────────────────────────────────

def _chunk_vue(lines: list[str]) -> list[dict]:
    """Split a Vue Single File Component at top-level block boundaries.

    Produces one chunk per SFC block (<template>, <script>, <style>) plus a
    preamble chunk for any content before the first block.  Falls back to the
    paragraph chunker if no SFC blocks are found.
    """
    source = "\n".join(lines)
    matches = list(_VUE_BLOCK.finditer(source))

    if not matches:
        return _chunk_by_paragraphs(lines)

    chunks = []

    def _emit(start_idx: int, end_idx: int, symbol: str | None, chunk_type: str) -> None:
        text = "\n".join(lines[start_idx:end_idx]).strip()
        if text:
            chunks.append({
                "text": text,
                "start_line": start_idx + 1,
                "end_line": end_idx,
                "symbol": symbol,
                "chunk_type": chunk_type,
            })

    # Preamble (before first SFC block — uncommon but valid)
    first_line = source[: matches[0].start()].count("\n")
    _emit(0, first_line, None, "block")

    for i, m in enumerate(matches):
        start_line = source[: m.start()].count("\n")
        end_line = source[: matches[i + 1].start()].count("\n") if i + 1 < len(matches) else len(lines)
        tag_name = m.group(1).lstrip("<").split()[0].rstrip(">").lower()
        chunk_type = "component" if tag_name == "template" else "function" if tag_name == "script" else "block"
        _emit(start_line, end_line, m.group(1).strip(), chunk_type)

    return chunks or _chunk_by_paragraphs(lines)


# ── Markdown heading chunker ──────────────────────────────────────────────────

def _chunk_markdown(lines: list[str]) -> list[dict]:
    """Split a Markdown file at heading boundaries (#, ##, ###, …).
    Content before the first heading is emitted as a preamble block."""
    source = "\n".join(lines)
    matches = list(_MD_HEADING.finditer(source))

    if not matches:
        return _chunk_by_paragraphs(lines)

    chunks = []

    def _emit(start_idx: int, end_idx: int, symbol: str | None) -> None:
        text = "\n".join(lines[start_idx:end_idx]).strip()
        if text:
            chunks.append({
                "text": text,
                "start_line": start_idx + 1,
                "end_line": end_idx,
                "symbol": symbol,
                "chunk_type": "heading",
            })

    # Preamble (before first heading)
    first_line = source[: matches[0].start()].count("\n")
    _emit(0, first_line, None)

    for i, m in enumerate(matches):
        start_line = source[: m.start()].count("\n")
        end_line = source[: matches[i + 1].start()].count("\n") if i + 1 < len(matches) else len(lines)
        _emit(start_line, end_line, m.group(0).strip())

    return chunks or _chunk_by_paragraphs(lines)


# ── SQL statement chunker ─────────────────────────────────────────────────────

def _chunk_sql(content: str, lines: list[str]) -> list[dict]:
    """Split SQL content at statement boundaries (semicolons).
    Groups consecutive short statements into one chunk to avoid producing
    hundreds of tiny single-line chunks for INSERT-heavy migration files."""
    MAX_LINES_PER_CHUNK = 80

    def _symbol_for(text: str) -> str | None:
        """First CREATE TABLE/VIEW/INDEX/FUNCTION/PROCEDURE/TRIGGER name found
        in this chunk's text, e.g. "CREATE TABLE users" — None if the chunk
        has no CREATE statement (the common case for INSERT-heavy chunks).
        Normalises "UNIQUE INDEX" to "INDEX" so the kind is always a single
        word (indexer.py's symbol_index fallback splits on the first space)."""
        m = _SQL_CREATE_NAME.search(_SQL_LINE_COMMENT.sub("", text))
        if not m:
            return None
        kind = "INDEX" if "INDEX" in m.group(1).upper() else m.group(1).upper()
        return f"CREATE {kind} {m.group(2)}"

    stmt_ends: list[int] = []  # 0-based line indexes of each ";"
    for m in _SQL_STMT_END.finditer(content):
        stmt_ends.append(content[: m.end()].count("\n"))

    if not stmt_ends:
        return _chunk_by_paragraphs(lines)

    chunks = []
    prev = 0
    buf_start = 0
    buf_lines = 0

    for end in stmt_ends:
        seg_len = end - prev + 1
        if buf_lines + seg_len > MAX_LINES_PER_CHUNK and buf_lines > 0:
            text = "\n".join(lines[buf_start:prev]).strip()
            if text:
                chunks.append({
                    "text": text,
                    "start_line": buf_start + 1,
                    "end_line": prev,
                    "symbol": _symbol_for(text),
                    "chunk_type": "statement",
                })
            buf_start = prev
            buf_lines = 0
        buf_lines += seg_len
        prev = end + 1

    text = "\n".join(lines[buf_start:]).strip()
    if text:
        chunks.append({
            "text": text,
            "start_line": buf_start + 1,
            "end_line": len(lines),
            "symbol": _symbol_for(text),
            "chunk_type": "statement",
        })

    return chunks or _chunk_by_paragraphs(lines)


# ── YAML / TOML top-level-key chunker ────────────────────────────────────────

def _chunk_yaml(lines: list[str]) -> list[dict]:
    """Split YAML/TOML at top-level key lines.
    A "top-level key" is a non-empty, non-comment line that starts at column 0
    and contains a colon (YAML) or equals sign (TOML)."""
    source = "\n".join(lines)
    matches = list(_YAML_KEY.finditer(source))

    if not matches:
        return _chunk_by_paragraphs(lines)

    chunks = []

    def _emit(start_idx: int, end_idx: int, symbol: str | None) -> None:
        text = "\n".join(lines[start_idx:end_idx]).strip()
        if text:
            chunks.append({
                "text": text,
                "start_line": start_idx + 1,
                "end_line": end_idx,
                "symbol": symbol,
                "chunk_type": "block",
            })

    first_line = source[: matches[0].start()].count("\n")
    _emit(0, first_line, None)

    for i, m in enumerate(matches):
        start_line = source[: m.start()].count("\n")
        end_line = source[: matches[i + 1].start()].count("\n") if i + 1 < len(matches) else len(lines)
        _emit(start_line, end_line, m.group(1).strip())

    return chunks or _chunk_by_paragraphs(lines)
