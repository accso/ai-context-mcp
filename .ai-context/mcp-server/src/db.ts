import { Pool } from "pg";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface IndexStatus {
  project_key:       string;
  file_count:        number;
  chunk_count:       number;
  files_with_l1:     number;
  newest_indexed_at: string | null;
}

export interface SymbolEntry {
  file_path:     string;
  symbol_name:   string;
  symbol_kind:   string;
  parent_symbol: string | null;
  signature:     string | null;
  start_line:    number;
  end_line:      number;
  chunk_id:      string | null;
}

// ── Project-scoped query helpers ──────────────────────────────────────────────
// Each helper accepts a Pool instance so callers can keep one Pool per project.

export async function getL0(pool: Pool, projectKey: string): Promise<string | null> {
  const res = await pool.query(
    "SELECT map_json->>'raw_text' AS raw_text FROM l0_maps WHERE project_key = $1",
    [projectKey]
  );
  return res.rows[0]?.raw_text ?? null;
}

export async function getSourcesL0(pool: Pool, sourceKeys: string[]): Promise<Record<string, string>> {
  if (!sourceKeys.length) return {};
  const res = await pool.query(
    "SELECT project_key, map_json->>'raw_text' AS raw_text FROM l0_maps WHERE project_key = ANY($1)",
    [sourceKeys]
  );
  const map: Record<string, string> = {};
  for (const row of res.rows) map[row.project_key] = row.raw_text;
  return map;
}

export async function getL1Summaries(
  pool: Pool,
  projectKey: string,
  filePaths: string[]
): Promise<Record<string, string>> {
  if (!filePaths.length) return {};
  const res = await pool.query(
    `SELECT fi.relative_path, ls.summary
     FROM file_index fi
     JOIN l1_summaries ls
       ON  ls.project_key   = fi.project_key
       AND ls.relative_path = fi.relative_path
       AND ls.content_sha256 = fi.content_sha256
     WHERE fi.project_key = $1
       AND fi.relative_path = ANY($2)`,
    [projectKey, filePaths]
  );
  const map: Record<string, string> = {};
  for (const row of res.rows) map[row.relative_path] = row.summary;
  return map;
}

export async function getSourceL1Summaries(
  pool: Pool,
  sourceKey: string,
  filePaths: string[],
  scope?: string
): Promise<Record<string, string>> {
  let res;
  if (filePaths.length) {
    res = await pool.query(
      `SELECT fi.relative_path, ls.summary
       FROM file_index fi
       JOIN l1_summaries ls
         ON  ls.project_key    = fi.project_key
         AND ls.relative_path  = fi.relative_path
         AND ls.content_sha256 = fi.content_sha256
       WHERE fi.project_key = $1
         AND fi.relative_path = ANY($2)
         ${scope ? "AND fi.relative_path LIKE $3" : ""}`,
      scope ? [sourceKey, filePaths, scope + "%"] : [sourceKey, filePaths]
    );
  } else {
    res = await pool.query(
      `SELECT fi.relative_path, ls.summary
       FROM file_index fi
       JOIN l1_summaries ls
         ON  ls.project_key    = fi.project_key
         AND ls.relative_path  = fi.relative_path
         AND ls.content_sha256 = fi.content_sha256
       WHERE fi.project_key = $1
         AND fi.relative_path LIKE $2
       ORDER BY fi.last_indexed_at DESC
       LIMIT 20`,
      [sourceKey, (scope ?? "") + "%"]
    );
  }
  const map: Record<string, string> = {};
  for (const row of res.rows) map[row.relative_path] = row.summary;
  return map;
}

export async function getIndexStatus(pool: Pool, projectKey: string): Promise<IndexStatus> {
  const [statsRes, l1Res] = await Promise.all([
    pool.query(
      `SELECT
         COUNT(*)                          AS file_count,
         COALESCE(SUM(chunk_count), 0)     AS chunk_count,
         MAX(last_indexed_at)              AS newest_indexed_at
       FROM file_index
       WHERE project_key = $1`,
      [projectKey]
    ),
    pool.query(
      `SELECT COUNT(*) AS files_with_l1
       FROM file_index fi
       WHERE fi.project_key = $1
         AND EXISTS (
           SELECT 1 FROM l1_summaries ls
           WHERE ls.project_key    = fi.project_key
             AND ls.relative_path  = fi.relative_path
             AND ls.content_sha256 = fi.content_sha256
         )`,
      [projectKey]
    ),
  ]);
  const s = statsRes.rows[0];
  return {
    project_key:        projectKey,
    file_count:         parseInt(s.file_count,   10),
    chunk_count:        parseInt(s.chunk_count,  10),
    files_with_l1:      parseInt(l1Res.rows[0].files_with_l1, 10),
    newest_indexed_at:  s.newest_indexed_at
                          ? new Date(s.newest_indexed_at).toISOString()
                          : null,
  };
}

export async function getRecentChanges(
  pool: Pool,
  projectKey: string,
  limit = 10
): Promise<Array<{ file_path: string; indexed_at: string; chunk_count: number }>> {
  const res = await pool.query(
    `SELECT relative_path AS file_path, last_indexed_at AS indexed_at, chunk_count
     FROM file_index
     WHERE project_key = $1
     ORDER BY last_indexed_at DESC
     LIMIT $2`,
    [projectKey, limit]
  );
  return res.rows;
}

export async function getProjectStructure(
  pool: Pool,
  projectKey: string
): Promise<Array<{ file_path: string; language: string; chunk_count: number }>> {
  const res = await pool.query(
    `SELECT relative_path AS file_path, language, chunk_count
     FROM file_index
     WHERE project_key = $1
     ORDER BY relative_path`,
    [projectKey]
  );
  return res.rows;
}

// ── Structural roles (see "Structural roles on `repo_structure`" in
// docs/architecture-deep-dive.md) ─────────────
// Degree-only, no embeddings/LLM. "Bridge" is a deliberately simple proxy
// (connector: both in- and out-edges, not a hub) — not full betweenness/
// community detection, a deliberate scope decision (see that doc).

export type StructuralRole = "hub" | "bridge" | "leaf";

export interface StructuralRoleInfo {
  role:      StructuralRole | null; // null = ordinary node, no strong signal either way
  inDegree:  number;
  outDegree: number;
}

const HUB_PERCENTILE = 0.10; // top 10% of files by in-degree

export async function getStructuralRoles(
  pool: Pool,
  projectKey: string
): Promise<Map<string, StructuralRoleInfo>> {
  // File-level degree = distinct connected files, combining import_edges
  // (already file-to-file) and call_edges (symbol-to-symbol, joined up to
  // the callee's file). Only resolution='exact' call edges count — an
  // ambiguous/unresolved edge isn't a confirmed connection.
  const res = await pool.query(
    `WITH edges AS (
       SELECT importer_file AS from_file, resolved_file AS to_file
       FROM import_edges
       WHERE project_key = $1 AND resolved_file IS NOT NULL
       UNION ALL
       SELECT ce.caller_file AS from_file, s.file_path AS to_file
       FROM call_edges ce
       JOIN symbol_index s ON s.id = ce.callee_symbol_id
       WHERE ce.project_key = $1 AND ce.resolution = 'exact'
     ),
     distinct_edges AS (
       SELECT DISTINCT from_file, to_file FROM edges WHERE from_file != to_file
     ),
     out_deg AS (SELECT from_file AS file, COUNT(*) AS n FROM distinct_edges GROUP BY from_file),
     in_deg  AS (SELECT to_file   AS file, COUNT(*) AS n FROM distinct_edges GROUP BY to_file)
     SELECT f.relative_path AS file_path,
            COALESCE(o.n, 0) AS out_degree,
            COALESCE(i.n, 0) AS in_degree
     FROM file_index f
     LEFT JOIN out_deg o ON o.file = f.relative_path
     LEFT JOIN in_deg  i ON i.file = f.relative_path
     WHERE f.project_key = $1`,
    [projectKey]
  );

  const rows = res.rows as Array<{ file_path: string; in_degree: number; out_degree: number }>;

  const inDegrees = rows.map((r) => Number(r.in_degree)).filter((n) => n > 0).sort((a, b) => b - a);
  const hubRank = Math.max(0, Math.ceil(inDegrees.length * HUB_PERCENTILE) - 1);
  const hubThreshold = inDegrees.length ? inDegrees[hubRank] : Infinity;

  const result = new Map<string, StructuralRoleInfo>();
  for (const r of rows) {
    const inDegree = Number(r.in_degree);
    const outDegree = Number(r.out_degree);
    let role: StructuralRole | null = null;
    if (inDegree === 0) {
      role = "leaf";
    } else if (inDegree >= hubThreshold && hubThreshold > 0) {
      role = "hub";
    } else if (inDegree >= 1 && outDegree >= 1) {
      role = "bridge";
    }
    result.set(r.file_path, { role, inDegree, outDegree });
  }
  return result;
}

export async function listDirectory(
  pool: Pool,
  projectKey: string,
  dirPath: string
): Promise<Array<{ file_path: string; language: string; chunk_count: number }>> {
  const prefix = dirPath.endsWith("/") ? dirPath : dirPath + "/";
  const res = await pool.query(
    `SELECT relative_path AS file_path, language, chunk_count
     FROM file_index
     WHERE project_key = $1 AND relative_path LIKE $2
     ORDER BY relative_path`,
    [projectKey, prefix + "%"]
  );
  return res.rows;
}

export async function lookupSymbol(
  pool: Pool,
  projectKey:  string,
  symbolName:  string,
  limit = 10
): Promise<SymbolEntry[]> {
  try {
    const res = await pool.query(
      `SELECT file_path, symbol_name, symbol_kind, parent_symbol,
              signature, start_line, end_line, chunk_id
       FROM   symbol_index
       WHERE  project_key = $1 AND lower(symbol_name) LIKE lower($2)
       ORDER  BY symbol_name
       LIMIT  $3`,
      [projectKey, `%${symbolName}%`, limit]
    );
    return res.rows as SymbolEntry[];
  } catch {
    return [];
  }
}

// ── analyze_impact (see "Call & Import Graph" in docs/architecture-deep-dive.md) ──
// Traversal/blast-radius tool built on call_edges/import_edges. Name-based
// resolution only (see the same doc) — not full type resolution,
// so "ambiguous" edges are surfaced rather than silently guessed at.

export interface ImpactEdge {
  symbol:    string;
  file:      string;
  line:      number;
  hops:      number;
}

export interface AmbiguousEdge {
  callee_name: string;
  candidates:  string[];   // "file:symbol"
}

export interface ImpactResult {
  symbol: { name: string; file: string; start_line: number; end_line: number } | null;
  callers: ImpactEdge[];
  callees: ImpactEdge[];
  ambiguous_edges: AmbiguousEdge[];
  affected_test_files: string[];
  blast_radius_summary: string;
}

// Co-located test-file heuristic (not coverage-tool integration) — see §1.4.
const TEST_FILE_PATTERN = /(\.(test|spec)\.[^/]+$)|(\/__tests__\/)|(\/tests\/)/i;

async function findSymbolCandidatesByName(
  pool: Pool, projectKey: string, name: string, limit = 20
): Promise<string[]> {
  const res = await pool.query(
    `SELECT file_path, symbol_name
     FROM symbol_index
     WHERE project_key = $1 AND symbol_name = $2
     ORDER BY file_path
     LIMIT $3`,
    [projectKey, name, limit]
  );
  return res.rows.map((r) => `${r.file_path}:${r.symbol_name}`);
}

async function bfsCallers(
  pool: Pool, projectKey: string, targetId: number, maxDepth: number,
  out: ImpactEdge[], ambiguousOut: AmbiguousEdge[]
): Promise<void> {
  let frontier = [targetId];
  const visited = new Set<number>([targetId]);
  for (let hop = 1; hop <= maxDepth && frontier.length > 0; hop++) {
    const res = await pool.query(
      `SELECT ce.caller_symbol_id, s.symbol_name, s.file_path, ce.call_line
       FROM call_edges ce
       JOIN symbol_index s ON s.id = ce.caller_symbol_id
       WHERE ce.project_key = $1 AND ce.callee_symbol_id = ANY($2::bigint[])`,
      [projectKey, frontier]
    );
    const next: number[] = [];
    for (const row of res.rows) {
      out.push({ symbol: row.symbol_name, file: row.file_path, line: row.call_line, hops: hop });
      if (!visited.has(row.caller_symbol_id)) {
        visited.add(row.caller_symbol_id);
        next.push(row.caller_symbol_id);
      }
    }
    frontier = next;
  }
}

async function bfsCallees(
  pool: Pool, projectKey: string, targetId: number, maxDepth: number,
  out: ImpactEdge[], ambiguousOut: AmbiguousEdge[]
): Promise<void> {
  let frontier = [targetId];
  const visited = new Set<number>([targetId]);
  for (let hop = 1; hop <= maxDepth && frontier.length > 0; hop++) {
    const res = await pool.query(
      `SELECT ce.callee_symbol_id, ce.callee_name, ce.resolution, ce.call_line,
              s.symbol_name AS callee_symbol_name, s.file_path AS callee_file
       FROM call_edges ce
       LEFT JOIN symbol_index s ON s.id = ce.callee_symbol_id
       WHERE ce.project_key = $1 AND ce.caller_symbol_id = ANY($2::bigint[])`,
      [projectKey, frontier]
    );
    const next: number[] = [];
    for (const row of res.rows) {
      if (row.resolution === "ambiguous") {
        const candidates = await findSymbolCandidatesByName(pool, projectKey, row.callee_name);
        ambiguousOut.push({ callee_name: row.callee_name, candidates });
        continue;
      }
      if (row.callee_symbol_id == null) continue; // unresolved — external/builtin/dynamic
      out.push({ symbol: row.callee_symbol_name, file: row.callee_file, line: row.call_line, hops: hop });
      if (!visited.has(row.callee_symbol_id)) {
        visited.add(row.callee_symbol_id);
        next.push(row.callee_symbol_id);
      }
    }
    frontier = next;
  }
}

async function collectAffectedTestFiles(
  pool: Pool, projectKey: string, targetFile: string, symbolName: string
): Promise<string[]> {
  const out = new Set<string>();

  const importRows = await pool.query(
    `SELECT DISTINCT importer_file FROM import_edges WHERE project_key = $1 AND resolved_file = $2`,
    [projectKey, targetFile]
  );
  for (const row of importRows.rows) {
    if (TEST_FILE_PATTERN.test(row.importer_file)) out.add(row.importer_file);
  }

  const callRows = await pool.query(
    `SELECT DISTINCT caller_file FROM call_edges WHERE project_key = $1 AND callee_name = $2`,
    [projectKey, symbolName]
  );
  for (const row of callRows.rows) {
    if (TEST_FILE_PATTERN.test(row.caller_file)) out.add(row.caller_file);
  }

  return [...out].sort();
}

export async function analyzeImpact(
  pool: Pool,
  projectKey:   string,
  symbolName:   string,
  filePath:     string | undefined,
  direction:    "callers" | "callees" | "both",
  depth:        number,
  includeTests: boolean
): Promise<ImpactResult & { error?: string }> {
  const params: unknown[] = [projectKey, symbolName];
  let query = `SELECT id, symbol_name, file_path, start_line, end_line
               FROM symbol_index WHERE project_key = $1 AND symbol_name = $2`;
  if (filePath) {
    query += ` AND file_path = $3`;
    params.push(filePath);
  }
  query += ` ORDER BY file_path, start_line LIMIT 5`;
  const matches = (await pool.query(query, params)).rows;

  const empty: ImpactResult = {
    symbol: null, callers: [], callees: [], ambiguous_edges: [], affected_test_files: [],
    blast_radius_summary: "",
  };

  if (matches.length === 0) {
    return {
      ...empty,
      blast_radius_summary: `No symbol named '${symbolName}' found${filePath ? ` in ${filePath}` : ""}.`,
      error: "not_found",
    };
  }
  if (matches.length > 1) {
    const list = matches.map((m) => `${m.file_path}:${m.start_line}`).join(", ");
    return {
      ...empty,
      blast_radius_summary: `Multiple symbols named '${symbolName}' found — pass file_path to disambiguate: ${list}`,
      error: "ambiguous_target",
    };
  }

  const target = matches[0];
  const callers: ImpactEdge[] = [];
  const callees: ImpactEdge[] = [];
  const ambiguousEdges: AmbiguousEdge[] = [];

  if (direction === "callers" || direction === "both") {
    await bfsCallers(pool, projectKey, target.id, depth, callers, ambiguousEdges);
    // Ambiguous calls that *might* target this symbol (unresolved due to
    // multiple same-named candidates) — surfaced separately since they
    // can't be attributed via callee_symbol_id (see §1.3).
    const ambigCallers = await pool.query(
      `SELECT ce.callee_name FROM call_edges ce
       WHERE ce.project_key = $1 AND ce.resolution = 'ambiguous' AND ce.callee_name = $2
       LIMIT 1`,
      [projectKey, target.symbol_name]
    );
    if (ambigCallers.rows.length > 0) {
      const candidates = await findSymbolCandidatesByName(pool, projectKey, target.symbol_name);
      ambiguousEdges.push({ callee_name: target.symbol_name, candidates });
    }
  }
  if (direction === "callees" || direction === "both") {
    await bfsCallees(pool, projectKey, target.id, depth, callees, ambiguousEdges);
  }

  const affectedTestFiles = includeTests
    ? await collectAffectedTestFiles(pool, projectKey, target.file_path, target.symbol_name)
    : [];

  const callerFiles = new Set(callers.map((c) => c.file));
  const filesWithoutTests = [...callerFiles].filter(
    (f) => !affectedTestFiles.some((t) => t.includes(f.replace(/\.[^/.]+$/, "")))
  ).length;
  const summaryParts = [`${callers.length} caller(s) across ${callerFiles.size} file(s)`];
  if (includeTests) {
    summaryParts.push(
      filesWithoutTests > 0
        ? `${filesWithoutTests} file(s) have no matching test file`
        : `all caller files have a matching test file`
    );
  }

  return {
    symbol: {
      name: target.symbol_name, file: target.file_path,
      start_line: target.start_line, end_line: target.end_line,
    },
    callers, callees,
    ambiguous_edges: ambiguousEdges,
    affected_test_files: affectedTestFiles,
    blast_radius_summary: summaryParts.join(", "),
  };
}
