"""
indexer.py
Full-index and delta-index logic.
  run_full_index()  - indexes every eligible file in the project
  index_file()      - indexes a single file (used by watcher on change)
  remove_file()     - removes a deleted file from the index

Paths:
  - project_root = /workspace  (already scoped to the project's root_path by the Docker
                                 volume mount in docker-compose.yml / start.sh)
  - relative_path = path relative to project_root  (stored in DB and Qdrant)

Features:
  - Chunk ID stability: content-hash UUID v5 IDs → delta upsert saves re-embedding
    unchanged chunks when only part of a file changes.
  - Symbol index: tree-sitter based AST extraction stored in symbol_index PostgreSQL table.
"""
import os, re, pathspec, logging
from pathlib import Path
import config_loader as cfg
import db, chunker, embedder
from llm_summariser import summarise_file, LLMClient
import doc_tree_builder
from symbol_extractor import extract_symbols_and_calls, is_supported as _ts_supported
from import_extractor import extract_imports, resolve_import

log = logging.getLogger(__name__)

# ── Skip list ─────────────────────────────────────────────────────────────────
# Single hardcoded source of truth — also used by watcher.py's live event
# handler via should_skip() below, so both agree. Keep this in sync with the
# "always skips" list documented in config.yaml's extra_skip_paths comment
# and README.md's "Eligibility filter" section.
ALWAYS_SKIP = {
    "build", ".gradle", "node_modules", ".ai-context", ".git",
    "dist", "out", "target", "__pycache__", ".idea", ".vscode",
    "*.class", "*.jar", "*.lock", ".kotlin", "generated", "generated-sources",
}


def load_gitignore(root: Path) -> pathspec.PathSpec | None:
    """
    Build one merged pathspec from this project's own root/.gitignore PLUS
    any configured indexer.extra_gitignore_paths files (see
    config_loader.extra_gitignore_container_paths() — bind-mounted into this
    container by start.sh at fixed paths, since they may live outside this
    project's own root_path, e.g. a monorepo top-level .gitignore).

    Merge semantics: the union of all patterns applies (a path is skipped if
    ANY one of the gitignore files would ignore it) — patterns are simply
    concatenated and compiled as one pathspec, not evaluated file-by-file.

    Caveat (documented in config.yaml/README too): gitignore's own semantics
    normally anchor a leading-'/' pattern to the directory of the .gitignore
    file it came from. Once merged, ALL patterns here are matched against
    paths relative to *this project's* root — so an anchored pattern from an
    extra gitignore file is interpreted relative to this project's root, not
    that file's own directory. Un-anchored patterns (the common case, e.g.
    `node_modules/` without a leading slash) are unaffected and match
    correctly regardless of which file they came from.
    """
    lines: list[str] = []
    gi = root / ".gitignore"
    if gi.exists():
        lines.extend(gi.read_text().splitlines())
    for extra_path in cfg.extra_gitignore_container_paths():
        p = Path(extra_path)
        if p.exists():
            lines.extend(p.read_text().splitlines())
        else:
            log.warning(f"[gitignore] Configured extra_gitignore_paths entry not found in container: {extra_path}")
    if not lines:
        return None
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def should_skip(rel_path: Path, gitignore: pathspec.PathSpec | None,
                extra_skip: list[str]) -> str | None:
    """
    Single source of truth for path-exclusion decisions — shared by the
    full-index scan (below) AND the live watcher's per-event handler
    (watcher.py's _DebounceHandler._eligible), so both always agree on what
    gets indexed.

    Returns the matched skip rule (e.g. "build", "extra_skip:secrets/",
    "gitignore:<pattern>") if the path should be skipped, or None if it is
    eligible. The rule name is used for coverage logging.
    """
    parts = set(rel_path.parts)
    for s in ALWAYS_SKIP:
        if s.startswith("*"):
            if rel_path.name.endswith(s[1:]):
                return s
        elif s in parts:
            return s
    for s in extra_skip:
        if rel_path.as_posix().startswith(s.lstrip("/")):
            return f"extra_skip:{s}"
    if gitignore and gitignore.match_file(str(rel_path)):
        # pathspec doesn't expose which pattern matched, so we label it generically.
        return "gitignore"
    return None


def _eligible_files(root: Path) -> list[Path]:
    """Return all eligible files under root (already the scoped project root)."""
    extensions = set(cfg.include_extensions())
    gitignore = load_gitignore(root)
    extra_skip = cfg.extra_skip_paths()
    result = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        skip_rule = should_skip(rel, gitignore, extra_skip)
        if skip_rule:
            # We only log skips for files whose extension would otherwise be indexed,
            # so the coverage log is focused on near-miss rules.
            if p.suffix.lower() in extensions or p.name in extensions:
                log.info(f"SKIPPED: {rel} (rule: {skip_rule})")
            continue
        if p.suffix.lower() in extensions or p.name in extensions:
            result.append(p)
    return result


def _detect_language(path: Path) -> str:
    ext = path.suffix.lower()
    mapping = {
        ".kt": "kotlin", ".kts": "kotlin",
        ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".html": "html", ".css": "css", ".scss": "scss",
        ".json": "json", ".yaml": "yaml", ".yml": "yaml",
        ".toml": "toml", ".sql": "sql", ".md": "markdown",
        ".py": "python", ".sh": "bash",
        ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
        ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    }
    return mapping.get(ext, "text")


def index_file(abs_path: str | Path, project_root: str | None = None,
               known_files: set[str] | None = None) -> bool:
    """
    Index a single file. Returns True if the file was (re)indexed, False if unchanged.
    relative_path is computed relative to project_root (the scoped root, not repo root).

    known_files: the project's full set of eligible relative paths, used for
    best-effort import resolution (import_extractor.resolve_import). Pass the
    pre-computed set from run_full_index() to avoid a DB round-trip per file;
    live single-file watch calls omit it and it's fetched lazily instead.
    """
    abs_path = Path(abs_path)
    root = Path(project_root or cfg.project_root())
    pkey = cfg.project_key()

    try:
        content = abs_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning(f"Cannot read {abs_path}: {e}")
        return False

    relative_path = str(abs_path.relative_to(root))
    content_sha = db.sha256(content)

    # ── Determine indexing mode (tree vs Qdrant) ───────────────────────────
    # Pass the full project config so chunk_file can read both doc_index
    # (for tree-index decisions) and indexer.max_chunk_chars.
    project_config = dict(cfg.get_project())
    # Keep chunk-size limit wired through the dedicated config_loader accessor.
    indexer_cfg = dict(project_config.get("indexer", {}))
    indexer_cfg["max_chunk_chars"] = cfg.max_chunk_chars()
    project_config["indexer"] = indexer_cfg
    doc_config = project_config.get("doc_index", {})
    chunks_result, use_tree_index = chunker.chunk_file(str(abs_path), content, project_config)

    if use_tree_index:
        # ── Doc tree indexing path ─────────────────────────────────────────
        existing_sha = db.get_doc_tree_sha(pkey, relative_path)
        existing_row = db.get_doc_tree(pkey, relative_path) if existing_sha == content_sha else None
        existing_tree = existing_row.get("tree_json") if existing_row else None
        if existing_sha == content_sha and existing_tree and _doc_tree_has_body_text(existing_tree):
            log.debug(f"Doc tree unchanged: {relative_path}")
            return False

        llm_client: LLMClient | None = None
        try:
            # Honour doc_index.tree_llm_model when set; fall back to shared.llm.model.
            tree_model_override = doc_config.get("tree_llm_model") or None
            llm_client = LLMClient(model_override=tree_model_override)
        except Exception:
            pass  # LLM unavailable — build tree without summaries

        try:
            tree = doc_tree_builder.build_tree_for_file(
                content, relative_path, llm_client, doc_config
            )
        except Exception as e:
            log.error(f"Doc tree build failed for {relative_path}: {e}")
            return False

        try:
            db.upsert_doc_tree(pkey, relative_path, content_sha, tree, tree["node_count"])
            # Also keep file_index in sync (chunk_count=0 for tree-indexed files)
            language = _detect_language(abs_path)
            db.upsert_file_record(pkey, relative_path, content_sha, language, 0,
                                  chunker_version=chunker.CHUNKER_VERSION)
        except Exception as e:
            log.error(f"Failed to store doc tree for {relative_path}: {e}")
            return False

        total_lines = len(content.splitlines())
        lines_covered = _doc_tree_lines_covered(tree.get("nodes", []))
        log.info(
            f"INDEXED: {relative_path} (doc_tree nodes: {tree['node_count']}, lines_covered: {lines_covered} of {total_lines})")
        return True

    # ── Standard Qdrant/L1 indexing path ──────────────────────────────────
    existing = db.get_file_record(pkey, relative_path)

    if existing and existing["content_sha256"] == content_sha and existing.get(
            "chunker_version") == chunker.CHUNKER_VERSION:
        # Content unchanged — but retry L1 if it was never successfully generated
        # (e.g. LM Studio was unavailable / returned Channel Error during initial index).
        if db.get_l1_summary(pkey, relative_path, content_sha) is None:
            _lang = _detect_language(abs_path)
            try:
                l1_summary = summarise_file(relative_path, content, _lang)
                db.upsert_l1_summary(pkey, relative_path, content_sha, l1_summary)
                log.info(f"Backfilled missing L1 summary: {relative_path}")
            except Exception as e:
                log.warning(f"L1 backfill failed for {relative_path}: {e}")
        return False  # L2 chunks are still current — no re-embedding needed

    language = _detect_language(abs_path)
    chunks = chunks_result
    if not chunks:
        return False

    try:
        vectors = embedder.embed_texts([c["text"] for c in chunks])
    except Exception as e:
        log.error(f"Embedding failed for {relative_path}: {e}")
        return False

    try:
        # ── Delta chunk update (stable content-hash IDs) ──────────────────────
        # Fetch existing IDs from Qdrant; only embed+insert new chunks, only
        # delete removed ones. Unchanged chunks keep their IDs → no re-embedding.
        existing_ids = db.get_chunk_ids_for_file(pkey, relative_path)
        added, deleted = db.upsert_chunks_delta(
            pkey, relative_path, language, chunks, vectors, existing_ids
        )
        if added or deleted:
            log.debug(f"chunk delta {relative_path}: +{added} -{deleted}")
    except Exception as e:
        log.error(f"Failed to store chunks for {relative_path}: {e}")
        return False

    # ── Symbol index: extract + store AST symbols ─────────────────────────────
    symbols: list[dict] = []
    raw_calls: list[dict] = []
    try:
        symbols, raw_calls = extract_symbols_and_calls(relative_path, content)
        if not symbols and not _ts_supported(relative_path):
            # Genuinely unsupported by tree-sitter here (not just "this
            # particular file happens to have zero symbols in a supported
            # language") — fall back to chunker-derived symbol/chunk_type
            # (see _fallback_symbols_from_chunks docstring for exactly which
            # languages this covers). No call-edge data available in this path.
            symbols = _fallback_symbols_from_chunks(chunks)
            raw_calls = []
            if symbols:
                log.debug(f"symbol_index fallback (no tree-sitter grammar): {relative_path} ({len(symbols)} symbols)")

        # Dispatch-arm ("case") chunks are never covered by tree-sitter's own
        # symbol extraction — no grammar here treats a switch/when/match arm
        # as a named symbol node in the first place, regardless of whether
        # the language is otherwise tree-sitter-supported. So always
        # additionally merge in fallback-derived symbols for chunk_type=="case"
        # chunks specifically (see chunker.py's dispatch-arm sub-chunking),
        # even for a fully tree-sitter-supported file whose other symbols
        # already came from extract_symbols() above. Deduplicated by
        # start_line against whatever's already in `symbols` — if the
        # whole-file fallback above already ran, it already covered these
        # same case chunks too, so this is a no-op in that case, not a
        # double-count.
        existing_start_lines = {s["start_line"] for s in symbols}
        case_chunks = [c for c in chunks if c["chunk_type"] == "case"]
        if case_chunks:
            case_symbols = [
                s for s in _fallback_symbols_from_chunks(case_chunks)
                if s["start_line"] not in existing_start_lines
            ]
            if case_symbols:
                symbols = symbols + case_symbols
                log.debug(f"symbol_index dispatch-arm symbols: {relative_path} ({len(case_symbols)} case arms)")

        if symbols:
            # Attach chunk_id to each symbol (find the chunk that contains the symbol)
            chunk_id_map = {
                db.stable_chunk_id(pkey, relative_path, c["text"], c["start_line"]): c
                for c in chunks
            }
            for sym in symbols:
                # Find the chunk whose line range best covers this symbol
                sym["chunk_id"] = _find_chunk_id_for_symbol(
                    sym["start_line"], chunk_id_map
                )
            db.upsert_symbols(pkey, relative_path, symbols)
            log.debug(f"symbols extracted: {relative_path} ({len(symbols)} symbols)")
    except Exception as e:
        log.warning(f"Symbol extraction failed for {relative_path}: {e}")

    # ── Import graph: extract + resolve import/using/require edges ────────────
    raw_edges: list[dict] = []
    try:
        raw_edges = extract_imports(relative_path, content)
        if raw_edges:
            files_for_resolution = known_files if known_files is not None else set(db.get_all_file_records(pkey).keys())
            for edge in raw_edges:
                edge["resolved_file"] = resolve_import(
                    relative_path, edge["specifier"], edge["import_kind"], files_for_resolution
                )
            db.upsert_import_edges(pkey, relative_path, raw_edges)
            log.debug(f"import edges extracted: {relative_path} ({len(raw_edges)} edges)")
        else:
            db.delete_import_edges(pkey, relative_path)
    except Exception as e:
        log.warning(f"Import edge extraction failed for {relative_path}: {e}")

    # ── Call graph: resolve raw_calls (from the symbol-extraction pass above)
    # to symbol_index IDs — see "Call & Import Graph" in docs/architecture-deep-dive.md.
    # Runs after the import block so import_edges.resolved_file is available
    # for cross-file callee resolution (step 2 of the resolution rule).
    try:
        if raw_calls:
            file_symbols = db.get_symbols_for_file(pkey, relative_path)
            # (name, start_line) -> id — exact caller attribution, keyed the
            # same way as symbol_index's own uniqueness constraint.
            by_name_line = {(s["symbol_name"], s["start_line"]): s["id"] for s in file_symbols}
            # name -> first matching id, for same-file callee lookup. Same-file
            # duplicate names (e.g. overloaded methods) resolve to whichever
            # is found first — an accepted approximation per §1.3's explicit
            # "name-based, not full type resolution" scope.
            by_name_first: dict[str, int] = {}
            for s in file_symbols:
                by_name_first.setdefault(s["symbol_name"], s["id"])

            reachable_files = sorted({
                e["resolved_file"] for e in raw_edges if e.get("resolved_file")
            })

            call_rows = []
            for call in raw_calls:
                caller_id = None
                if call["caller_symbol_name"] is not None:
                    caller_id = by_name_line.get(
                        (call["caller_symbol_name"], call["caller_start_line"])
                    )

                callee_id = by_name_first.get(call["callee_name"])
                if callee_id is not None:
                    resolution = "exact"
                else:
                    candidates = db.find_symbol_ids_by_name(
                        pkey, call["callee_name"], reachable_files
                    )
                    if len(candidates) == 1:
                        callee_id, resolution = candidates[0]["id"], "exact"
                    elif len(candidates) > 1:
                        # Ambiguous — don't store per-candidate rows (avoids
                        # combinatorial growth for common short names); the
                        # traversal tool re-queries symbol_index by name on
                        # demand instead (see §1.3).
                        callee_id, resolution = None, "ambiguous"
                    else:
                        callee_id, resolution = None, "unresolved"

                call_rows.append({
                    "caller_symbol_id": caller_id,
                    "callee_name":      call["callee_name"],
                    "callee_symbol_id": callee_id,
                    "resolution":       resolution,
                    "call_line":        call["call_line"],
                })

            db.upsert_call_edges(pkey, relative_path, call_rows)
            log.debug(f"call edges extracted: {relative_path} ({len(call_rows)} edges)")
        else:
            db.delete_call_edges(pkey, relative_path)
    except Exception as e:
        log.warning(f"Call edge extraction failed for {relative_path}: {e}")

    # ── L1 summary: check SHA-based cache first ────────────────────────────
    l1_summary = db.get_l1_summary(pkey, relative_path, content_sha)
    if l1_summary is None:
        try:
            l1_summary = summarise_file(relative_path, content, language)
            db.upsert_l1_summary(pkey, relative_path, content_sha, l1_summary)
        except Exception as e:
            log.warning(f"L1 summary failed for {relative_path}: {e}")

    try:
        db.upsert_file_record(pkey, relative_path, content_sha, language, len(chunks),
                              chunker_version=chunker.CHUNKER_VERSION)
    except Exception as e:
        log.error(f"Failed to update file record for {relative_path}: {e}")
        return False

    total_lines = len(content.splitlines())
    lines_covered = sum((c["end_line"] - c["start_line"] + 1) for c in chunks)
    log.info(f"INDEXED: {relative_path} (chunks: {len(chunks)}, lines_covered: {lines_covered} of {total_lines})")
    return True


def remove_file(abs_path: str | Path, project_root: str | None = None) -> None:
    root = Path(project_root or cfg.project_root())
    relative_path = str(Path(abs_path).relative_to(root))
    pkey = cfg.project_key()
    db.delete_file_chunks(pkey, relative_path)
    db.delete_file_record(pkey, relative_path)
    db.delete_symbols(pkey, relative_path)
    db.delete_import_edges(pkey, relative_path)
    db.delete_call_edges(pkey, relative_path)
    # Also remove doc tree if one exists for this path
    db.delete_doc_tree(pkey, relative_path)
    log.info(f"Removed {relative_path} from index.")


def _doc_tree_has_body_text(tree: dict) -> bool:
    """Return True if every node in the stored tree has a non-empty body_text field."""

    def check(nodes: list[dict]) -> bool:
        for n in nodes:
            if not n.get("body_text"):
                return False
            if not check(n.get("children", [])):
                return False
        return True

    return check(tree.get("nodes", []))


def _doc_tree_lines_covered(nodes: list[dict]) -> int:
    """Sum of (end_line - start_line + 1) across all doc-tree nodes."""
    total = 0
    for n in nodes:
        total += n.get("end_line", n.get("start_line", 0)) - n.get("start_line", 0) + 1
        total += _doc_tree_lines_covered(n.get("children", []))
    return total


def _find_chunk_id_for_symbol(symbol_start: int, chunk_id_map: dict) -> str | None:
    """Return the chunk_id of the chunk whose start_line is closest to symbol_start."""
    best_cid, best_dist = None, 999999
    for cid, chunk in chunk_id_map.items():
        if chunk["start_line"] <= symbol_start <= chunk["end_line"]:
            return cid  # exact containment
        dist = abs(chunk["start_line"] - symbol_start)
        if dist < best_dist:
            best_dist, best_cid = dist, cid
    return best_cid


# ── Symbol index fallback (for languages extract_symbols() doesn't cover) ──────
# extract_symbols() (tree-sitter) returns [] for any language outside its
# _EXT_TO_LANG map (Vue, SQL, YAML/TOML, C/C++, Go, Rust, Ruby, PHP, Markdown)
# — see symbol_extractor.py. chunker.py already derives a "symbol" string for
# most of those from its own regex boundaries (Vue's SFC tags, YAML/TOML's
# top-level keys, Markdown's headings, and — since this fallback was added —
# SQL's CREATE-statement names); this derives symbol_index rows straight from
# that instead of leaving symbol_index empty for these files. Languages the
# chunker itself has no structural boundaries for yet (falls through to the
# generic paragraph splitter — C/C++, Go, Rust, Ruby, PHP today) still get no
# rows here, same as before — that's a chunker-level gap, not something this
# fallback can manufacture data for; see chunker.py's module docstring for
# which extensions have real boundary-detection.

# A CREATE statement's chunk-level "symbol" looks like "CREATE TABLE users" —
# extract the specific object kind + bare name from it for a much more useful
# symbol_kind ("table"/"view"/"index"/"function"/"procedure"/"trigger")
# instead of the generic "statement" chunk_type every SQL chunk otherwise has.
_SQL_SYMBOL_RE = re.compile(r"^CREATE\s+(\w+)\s+(.+)$")

# Markdown headings retain their leading '#'s in chunk["symbol"] (e.g.
# "## Usage"); Vue's SFC tags retain their angle brackets (e.g.
# "<script setup>"). Strip both down to bare, searchable text.
_MD_HEADING_MARKS = re.compile(r"^#+\s*")


def _fallback_symbol_from_chunk(chunk: dict) -> tuple[str, str] | None:
    """
    Derive (symbol_kind, symbol_name) from a chunk's chunker-assigned
    "symbol"/"chunk_type" fields, for the symbol_index fallback path.
    Returns None if the chunk carries no usable symbol text.
    """
    symbol = chunk.get("symbol")
    if not symbol:
        return None

    sql_match = _SQL_SYMBOL_RE.match(symbol)
    if sql_match:
        return (sql_match.group(1).lower(), sql_match.group(2))

    name = _MD_HEADING_MARKS.sub("", symbol).strip("<>").strip()
    if not name:
        return None
    return (chunk["chunk_type"], name)


def _fallback_symbols_from_chunks(chunks: list[dict]) -> list[dict]:
    """
    Build symbol_index-ready dicts directly from chunk metadata, for files
    whose language extract_symbols() (tree-sitter) doesn't cover. See the
    fallback note above _SQL_SYMBOL_RE for scope/limitations.
    """
    results = []
    for chunk in chunks:
        derived = _fallback_symbol_from_chunk(chunk)
        if derived is None:
            continue
        symbol_kind, symbol_name = derived
        results.append({
            "symbol_name": symbol_name,
            "symbol_kind": symbol_kind,
            "parent_symbol": None,  # chunker doesn't track nesting
            # chunker doesn't capture full signatures, only leading declaration
            # lines — reuse the chunk's own first line as a best-effort one,
            # since it's already available at zero extra cost.
            "signature": chunk["text"].splitlines()[0][:200] if chunk["text"] else None,
            "start_line": chunk["start_line"],
            "end_line": chunk["end_line"],
            "chunk_id": None,  # filled in below, same as the tree-sitter path
        })
    return results


def run_full_index() -> None:
    """Index all eligible files. Called on first init and manual re-index."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = Path(cfg.project_root())
    log.info(f"[index] Scanning for eligible files in {root} ...")
    files = _eligible_files(root)
    total = len(files)
    log.info(f"[index] Found {total} eligible files — starting full index for '{cfg.project_key()}' ...")
    # Precompute the full set of eligible relative paths once for import
    # resolution (import_extractor.resolve_import), rather than querying
    # file_index per file. Built from the on-disk scan (not from file_index),
    # so it's correct even on a project's very first full index, before any
    # row exists in file_index yet.
    known_files = {str(p.relative_to(root)) for p in files}
    indexed = 0
    unchanged = 0
    i = 0
    try:
        for i, p in enumerate(files, 1):
            try:
                if index_file(p, str(root), known_files=known_files):
                    indexed += 1
                else:
                    unchanged += 1
            except Exception as e:
                log.error(f"[index] Error indexing {p}: {e}")
            # Progress every 10 files and on the last file
            if i % 10 == 0 or i == total:
                log.info(
                    f"[index] Progress: {i}/{total}  "
                    f"indexed={indexed}  unchanged={unchanged}"
                )
    except KeyboardInterrupt:
        log.info(
            f"[index] Interrupted at {i}/{total}. "
            f"indexed={indexed}  unchanged={unchanged}"
        )
        raise
    log.info(
        f"[index] Full index complete — "
        f"indexed={indexed}  unchanged={unchanged}  total={total}"
    )

    # ── Doc tree backfill: fill in summaries for trees built without LLM ──
    _backfill_doc_tree_summaries()


def _backfill_doc_tree_summaries() -> None:
    """Backfill summaries for doc tree nodes where LLM was unavailable on first build."""
    pkey = cfg.project_key()
    root = Path(cfg.project_root())
    trees_missing = db.get_doc_trees_with_null_summaries(pkey)
    if not trees_missing:
        return
    log.info(f"[index] Doc tree summary backfill: {len(trees_missing)} files with missing summaries")
    doc_config = cfg.get_project().get("doc_index", {})
    try:
        llm_client = LLMClient()
    except Exception as e:
        log.warning(f"[index] LLM unavailable for doc tree backfill: {e}")
        return
    for tree_meta in trees_missing:
        file_path = tree_meta["file_path"]
        abs_path = root / file_path
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            log.warning(f"[index] Cannot read {file_path} for backfill: {e}")
            continue
        try:
            updated_tree = doc_tree_builder.rebuild_summaries(
                tree_meta["tree_json"], text, llm_client, doc_config
            )
            db.upsert_doc_tree(
                pkey, file_path, tree_meta["content_sha256"],
                updated_tree, tree_meta["node_count"],
            )
            log.info(f"[index] Backfilled doc tree summaries: {file_path}")
        except Exception as e:
            log.warning(f"[index] Doc tree backfill failed for {file_path}: {e}")
