-- ─────────────────────────────────────────────────────────────────────────────
-- AI CONTEXT DB — PostgreSQL schema (multi-project)
-- Idempotent: safe to re-run. All tables use CREATE TABLE IF NOT EXISTS.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE
EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Project registry ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projects
(
    project_key
    TEXT
    PRIMARY
    KEY,
    display_name
    TEXT
    NOT
    NULL,
    profile
    TEXT
    NOT
    NULL,
    root_path
    TEXT
    NOT
    NULL, -- relative to repo root; "" = whole repo
    registered_at
    TIMESTAMPTZ
    DEFAULT
    now
(
),
    last_indexed_at TIMESTAMPTZ
    );

-- ── File index (change-detection + metadata) ──────────────────────────────────
-- relative_path is relative to the project's root_path (not repo root).
CREATE TABLE IF NOT EXISTS file_index
(
    project_key
    TEXT
    NOT
    NULL
    REFERENCES
    projects
(
    project_key
) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    language TEXT,
    chunk_count INTEGER DEFAULT 0,
    chunker_version INTEGER NOT NULL DEFAULT 1,
    last_indexed_at TIMESTAMPTZ DEFAULT now
(
),
    PRIMARY KEY
(
    project_key,
    relative_path
)
    );

-- ── L1 summaries (content-addressed LLM cache) ────────────────────────────────
-- Keyed by (project_key, relative_path, content_sha256).
-- Benefit: reverting a file reuses the cached summary; no duplicate LLM calls.
CREATE TABLE IF NOT EXISTS l1_summaries
(
    project_key
    TEXT
    NOT
    NULL
    REFERENCES
    projects
(
    project_key
) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    summary TEXT NOT NULL,
    generated_at TIMESTAMPTZ DEFAULT now
(
),
    PRIMARY KEY
(
    project_key,
    relative_path,
    content_sha256
)
    );

-- ── L0 maps (static project structure, one per project) ───────────────────────
-- map_json always contains a "raw_text" key for direct MCP injection.
CREATE TABLE IF NOT EXISTS l0_maps
(
    project_key
    TEXT
    PRIMARY
    KEY
    REFERENCES
    projects
(
    project_key
) ON DELETE CASCADE,
    map_json JSONB NOT NULL,
    generated_at TIMESTAMPTZ DEFAULT now
(
)
    );

-- ── Doc trees (PageIndex-style hierarchical tree per documentation file) ──────
-- Stores the tree index for large Markdown files (.md with ≥ min_headings_for_tree headings,
-- or files under always_tree_paths). These files are NOT chunked into Qdrant.
CREATE TABLE IF NOT EXISTS doc_trees
(
    id
    SERIAL
    PRIMARY
    KEY,
    project_key
    TEXT
    NOT
    NULL,
    file_path
    TEXT
    NOT
    NULL,
    content_sha256
    TEXT
    NOT
    NULL,
    tree_json
    JSONB
    NOT
    NULL, -- full hierarchical tree (raw_text stripped)
    node_count
    INTEGER
    NOT
    NULL
    DEFAULT
    0,
    generated_at
    TIMESTAMPTZ
    NOT
    NULL
    DEFAULT
    NOW
(
),
    UNIQUE
(
    project_key,
    file_path
)
    );

CREATE INDEX IF NOT EXISTS idx_doc_trees_project ON doc_trees (project_key);
CREATE INDEX IF NOT EXISTS idx_doc_trees_path ON doc_trees (project_key, file_path);

-- ── Symbol index (AST-extracted symbols, populated by watcher/symbol_extractor.py) ──
-- Each row is one named symbol (class, function, interface, method, …) found in a file.
-- Chunk ID links back to the Qdrant point that contains the symbol's source text.
CREATE TABLE IF NOT EXISTS symbol_index
(
    id
    BIGSERIAL
    PRIMARY
    KEY,
    project_key
    TEXT
    NOT
    NULL
    REFERENCES
    projects
(
    project_key
) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    symbol_kind TEXT NOT NULL, -- 'class' | 'function' | 'method' | 'interface' | …
    parent_symbol TEXT, -- enclosing class/namespace, if any
    signature TEXT, -- first line of the declaration (≤ 200 chars)
    start_line INT NOT NULL,
    end_line INT NOT NULL,
    chunk_id TEXT, -- stable Qdrant point UUID (v5 content-hash)
    indexed_at TIMESTAMPTZ DEFAULT NOW
(
),
    UNIQUE
(
    project_key,
    file_path,
    start_line,
    symbol_name
)
    );

CREATE INDEX IF NOT EXISTS idx_symbol_name ON symbol_index(project_key, lower (symbol_name));
CREATE INDEX IF NOT EXISTS idx_symbol_file ON symbol_index(project_key, file_path);
CREATE INDEX IF NOT EXISTS idx_symbol_kind ON symbol_index(project_key, symbol_kind);

-- ── Import graph (populated by watcher/import_extractor.py) ───────────────────
-- One row per import/using/require statement found in a file. resolved_file is
-- filled in with best-effort resolution against file_index (relative-path
-- resolution for relative specifiers, suffix matching for module/package
-- specifiers) — NULL means "no confident match" (e.g. external/library import),
-- which is recorded but never treated as a graph edge for traversal purposes.
-- See "Call & Import Graph" in docs/architecture-deep-dive.md.
CREATE TABLE IF NOT EXISTS import_edges
(
    id
    BIGSERIAL
    PRIMARY
    KEY,
    project_key
    TEXT
    NOT
    NULL
    REFERENCES
    projects
(
    project_key
) ON DELETE CASCADE,
    importer_file TEXT NOT NULL, -- relative_path of the file containing the import
    specifier TEXT NOT NULL, -- raw import string, e.g. "./user-service", "@myorg/pkg"
    resolved_file TEXT, -- relative_path if resolved to a file in file_index, else NULL
    import_kind TEXT NOT NULL, -- 'es_import' | 'cjs_require' | 'py_import' | 'java_import' | 'kotlin_import' | 'using' | 'swift_import'
    line INT NOT NULL,
    indexed_at TIMESTAMPTZ DEFAULT NOW
(
),
    UNIQUE
(
    project_key,
    importer_file,
    specifier,
    line
)
    );
CREATE INDEX IF NOT EXISTS idx_import_importer ON import_edges(project_key, importer_file);
CREATE INDEX IF NOT EXISTS idx_import_resolved ON import_edges(project_key, resolved_file);

-- ── Call graph (populated by a future extension of symbol_extractor.py) ───────
-- Schema added now alongside import_edges (same migration) so both land in one
-- pass; call_edges stays empty until call-edge extraction (plan §1.3) ships.
CREATE TABLE IF NOT EXISTS call_edges
(
    id
    BIGSERIAL
    PRIMARY
    KEY,
    project_key
    TEXT
    NOT
    NULL
    REFERENCES
    projects
(
    project_key
) ON DELETE CASCADE,
    caller_file TEXT NOT NULL,
    caller_symbol_id BIGINT REFERENCES symbol_index
(
    id
)
  ON DELETE CASCADE, -- enclosing symbol, NULL if module-level call
    callee_name TEXT NOT NULL, -- raw identifier text at the call site
    callee_symbol_id BIGINT REFERENCES symbol_index
(
    id
)
  ON DELETE SET NULL, -- resolved target, NULL if unresolved
    resolution TEXT NOT NULL, -- 'exact' | 'ambiguous' | 'unresolved'
    call_line INT NOT NULL,
    indexed_at TIMESTAMPTZ DEFAULT NOW
(
)
    );
CREATE INDEX IF NOT EXISTS idx_call_caller ON call_edges(project_key, caller_symbol_id);
CREATE INDEX IF NOT EXISTS idx_call_callee ON call_edges(project_key, callee_symbol_id);
CREATE INDEX IF NOT EXISTS idx_call_name ON call_edges(project_key, lower (callee_name));

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_file_index_project ON file_index(project_key);
CREATE INDEX IF NOT EXISTS idx_file_index_recent ON file_index(project_key, last_indexed_at DESC);
CREATE INDEX IF NOT EXISTS idx_l1_lookup ON l1_summaries(project_key, relative_path, content_sha256);

-- ── Convenience view ──────────────────────────────────────────────────────────
CREATE
OR REPLACE VIEW project_stats AS
SELECT p.project_key,
       p.display_name,
       p.profile,
       p.root_path,
       p.last_indexed_at,
       COUNT(f.relative_path)          AS file_count,
       COALESCE(SUM(f.chunk_count), 0) AS total_chunks
FROM projects p
         LEFT JOIN file_index f ON f.project_key = p.project_key
GROUP BY p.project_key, p.display_name, p.profile,
         p.root_path, p.last_indexed_at;
