"""
import_extractor.py
Regex-based per-language import/using/require statement extraction.

Deliberately NOT tree-sitter-based: import statements are far more regular
across languages than full declarations (single-line, keyword-anchored), so a
regex pass is simpler and keeps this usable even for languages
symbol_extractor.py has no grammar for. In this first increment, scope is
intentionally restricted to the same six languages symbol_extractor.py
already covers with real symbol extraction (Python, TypeScript/TSX,
JavaScript, Java, C#, Kotlin, Swift) rather than adding new languages
(e.g. Go) that symbol_extractor.py doesn't parse yet — see
"Call & Import Graph" in docs/architecture-deep-dive.md.

Each returned dict from extract_imports():
  {
    "specifier":   str,   # raw import string as written, e.g. "./user-service"
    "import_kind": str,   # 'es_import' | 'cjs_require' | 'py_import' |
                          # 'java_import' | 'kotlin_import' | 'using' | 'swift_import'
    "line":        int,   # 1-based
  }

resolve_import() is a separate, best-effort step (called by indexer.py once
per extracted edge, with the project's known relative_path set) — kept
separate from extraction so the regex pass never needs DB/filesystem access.
"""
from __future__ import annotations

import re
import posixpath
from pathlib import Path

# ── File extension → import "language" bucket ─────────────────────────────────
# Mirrors symbol_extractor.py's _EXT_TO_LANG scope exactly (six languages);
# TS/TSX/JS/JSX share one bucket since their import syntax is identical.
_EXT_TO_BUCKET: dict[str, str] = {
    ".py": "python",
    ".ts": "es",
    ".tsx": "es",
    ".js": "es",
    ".jsx": "es",
    ".java": "java",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
}

# Candidate file extensions to try when resolving a specifier that omits one,
# per bucket — used by resolve_import() below.
_CANDIDATE_EXTS: dict[str, list[str]] = {
    "es": [".ts", ".tsx", ".js", ".jsx"],
    "python": [".py"],
    "java": [".java"],
    "csharp": [".cs"],
    "kotlin": [".kt", ".kts"],
    "swift": [".swift"],
}

# ── Per-bucket regexes ─────────────────────────────────────────────────────────
_ES_IMPORT_FROM = re.compile(r"""^\s*import\s+(?:type\s+)?.*?\bfrom\s+['"]([^'"]+)['"]""")
_ES_IMPORT_BARE = re.compile(r"""^\s*import\s+['"]([^'"]+)['"]""")
_ES_EXPORT_FROM = re.compile(r"""^\s*export\s+(?:\*\s+as\s+\w+\s+|\{[^}]*\}\s+|\*\s+)?from\s+['"]([^'"]+)['"]""")
_ES_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")

_PY_IMPORT = re.compile(r"^\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)")
_PY_FROM_IMPORT = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+")

_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;")

_KOTLIN_IMPORT = re.compile(r"^\s*import\s+([\w.]+(?:\.\*)?)\s*$")

_CSHARP_USING = re.compile(r"^\s*using\s+(?:static\s+)?(?:\w+\s*=\s*)?([\w.]+)\s*;")

_SWIFT_IMPORT = re.compile(r"^\s*import\s+(?:\w+\s+)?(\w+(?:\.\w+)*)")


def is_supported(relative_path: str) -> bool:
    """Whether extract_imports() has a pattern set for this file's extension."""
    ext = Path(relative_path).suffix.lower()
    return ext in _EXT_TO_BUCKET


def extract_imports(relative_path: str, content: str) -> list[dict]:
    """
    Extract raw import/using/require statements from source content.
    Returns [] for files whose extension isn't one of the six supported
    languages (see module docstring) — no fallback, this is a v1 subset by
    design.
    """
    ext = Path(relative_path).suffix.lower()
    bucket = _EXT_TO_BUCKET.get(ext)
    if not bucket:
        return []

    edges: list[dict] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if bucket == "es":
            for pattern, kind in (
                    (_ES_IMPORT_FROM, "es_import"),
                    (_ES_IMPORT_BARE, "es_import"),
                    (_ES_EXPORT_FROM, "es_import"),
            ):
                m = pattern.match(line)
                if m:
                    edges.append({"specifier": m.group(1), "import_kind": kind, "line": lineno})
                    break
            for m in _ES_REQUIRE.finditer(line):
                edges.append({"specifier": m.group(1), "import_kind": "cjs_require", "line": lineno})
        elif bucket == "python":
            m = _PY_FROM_IMPORT.match(line)
            if m:
                edges.append({"specifier": m.group(1), "import_kind": "py_import", "line": lineno})
                continue
            m = _PY_IMPORT.match(line)
            if m:
                for spec in (s.strip() for s in m.group(1).split(",")):
                    if spec:
                        edges.append({"specifier": spec, "import_kind": "py_import", "line": lineno})
        elif bucket == "java":
            m = _JAVA_IMPORT.match(line)
            if m:
                edges.append({"specifier": m.group(1), "import_kind": "java_import", "line": lineno})
        elif bucket == "kotlin":
            m = _KOTLIN_IMPORT.match(line)
            if m:
                edges.append({"specifier": m.group(1), "import_kind": "kotlin_import", "line": lineno})
        elif bucket == "csharp":
            m = _CSHARP_USING.match(line)
            if m:
                edges.append({"specifier": m.group(1), "import_kind": "using", "line": lineno})
        elif bucket == "swift":
            m = _SWIFT_IMPORT.match(line)
            if m:
                edges.append({"specifier": m.group(1), "import_kind": "swift_import", "line": lineno})

    return edges


def resolve_import(
        importer_file: str,
        specifier: str,
        import_kind: str,
        known_files: set[str],
) -> str | None:
    """
    Best-effort resolution of a raw specifier to a relative_path already
    present in known_files (the project's full set of indexed/eligible
    relative paths). Returns None when no confident match is found — that's
    an expected, common outcome for external/library imports, not an error.

    Two strategies (see "Call & Import Graph" in docs/architecture-deep-dive.md):
      1. Relative specifiers ("./x", "../x") — resolve against the importer's
         own directory, trying each candidate extension for its bucket.
      2. Absolute/module specifiers (everything else) — a package-root guess:
         convert dots to path separators and check whether any known file's
         path ENDS WITH that candidate suffix (+ extension). No attempt at
         node_modules/external-package resolution.
    """
    ext = Path(importer_file).suffix.lower()
    bucket = _EXT_TO_BUCKET.get(ext)
    if not bucket:
        return None
    candidate_exts = _CANDIDATE_EXTS.get(bucket, [])

    if specifier.startswith("."):
        if bucket == "python":
            # Python relative-import dot syntax: leading dots count package
            # levels up from the importer's own directory (1 dot = current
            # package/directory, 2 = parent, ...), remaining dot-separated
            # segments are a package/module path, NOT a "./"-style path
            # fragment the way TS/JS relative specifiers work.
            n_dots = len(specifier) - len(specifier.lstrip("."))
            remainder = specifier[n_dots:]  # e.g. "utils.helpers" or "" for "from . import x"
            base = posixpath.dirname(importer_file)
            for _ in range(n_dots - 1):
                base = posixpath.dirname(base)
            seg_path = remainder.replace(".", "/") if remainder else ""
            joined = posixpath.normpath(posixpath.join(base, seg_path)) if seg_path else base
            candidates = [joined + ".py", posixpath.join(joined, "__init__.py")]
            for c in candidates:
                if c in known_files:
                    return c
            return None

        importer_dir = posixpath.dirname(importer_file)
        base = posixpath.normpath(posixpath.join(importer_dir, specifier))
        candidates = [base] if Path(base).suffix else [base + e for e in candidate_exts]
        # Also try as a directory-style index/module file (e.g. "./foo" -> "foo/index.ts").
        if not Path(base).suffix:
            candidates += [posixpath.join(base, "index" + e) for e in candidate_exts]
        for c in candidates:
            if c in known_files:
                return c
        return None

    # Absolute/module specifier: dots -> path separators, suffix-match.
    dotted = specifier.rstrip(".*")  # "com.foo.Bar.*" -> "com.foo.Bar"
    if not dotted:
        return None
    suffix_path = dotted.replace(".", "/")
    suffixes_to_try = [suffix_path] if Path(suffix_path).suffix else [suffix_path + e for e in candidate_exts]
    for known in known_files:
        for suffix in suffixes_to_try:
            if known == suffix or known.endswith("/" + suffix):
                return known
    return None
