import { Pool } from "pg";

// ── Types ────────────────────────────────────────────────────────────────────

export type DocTreeNode = {
  node_id: string;
  title: string;
  start_line: number;
  end_line: number;
  summary: string | null;
  body_text?: string;
  children: DocTreeNode[];
};

export interface DocNode {
  nodeId:    string;
  title:     string;
  filePath:  string;
  startLine: number;
  endLine:   number;
  summary:   string | null;
  text?:     string;           // populated only in node_text mode
}

export interface DocTreeResult {
  filePath:       string;
  toc:            string;     // formatted table-of-contents string
  relevantNodes:  DocNode[];
  estimatedTokens: number;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

/** Flatten a tree's nodes array into a TOC string. */
function buildToc(filePath: string, treeJson: Record<string, unknown>): string {
  const description = (treeJson.doc_description as string | null) ?? "";
  const nodes = (treeJson.nodes ?? []) as Record<string, unknown>[];

  function flattenNodes(nodes: Record<string, unknown>[], indent = 0): string[] {
    const lines: string[] = [];
    const prefix = "  ".repeat(indent);
    for (const n of nodes) {
      const summary = n.summary ? `: "${n.summary}"` : "";
      lines.push(
        `${prefix}[${n.node_id}] ${n.title} (lines ${n.start_line}–${n.end_line})${summary}`
      );
      const children = (n.children ?? []) as Record<string, unknown>[];
      lines.push(...flattenNodes(children, indent + 1));
    }
    return lines;
  }

  const header = `# ${filePath}` + (description ? `\n_${description}_` : "");
  return header + "\n" + flattenNodes(nodes).join("\n");
}

/** Find a node by ID in the (recursive) nodes array. */
function findNodeById(
  nodes: Record<string, unknown>[],
  nodeId: string
): Record<string, unknown> | null {
  for (const n of nodes) {
    if (n.node_id === nodeId) return n;
    const found = findNodeById(
      (n.children ?? []) as Record<string, unknown>[],
      nodeId
    );
    if (found) return found;
  }
  return null;
}


// ── DB queries ────────────────────────────────────────────────────────────────

/** Return all tree rows for a project (file_path + tree_json). */
async function getAllDocTrees(
  pool: Pool,
  projectKey: string
): Promise<Array<{ file_path: string; tree_json: Record<string, unknown>; node_count: number }>> {
  const res = await pool.query(
    `SELECT file_path, tree_json, node_count
     FROM doc_trees
     WHERE project_key = $1
     ORDER BY file_path`,
    [projectKey]
  );
  return res.rows;
}

/** Return a single doc tree by file path. */
export async function getDocTreeByPath(
  pool: Pool,
  projectKey: string,
  filePath: string
): Promise<{ file_path: string; tree_json: Record<string, unknown> } | null> {
  const res = await pool.query(
    `SELECT file_path, tree_json FROM doc_trees WHERE project_key = $1 AND file_path = $2`,
    [projectKey, filePath]
  );
  return res.rows[0] ?? null;
}

/**
 * Find the most specific doc-tree node whose line range contains the requested
 * range. If no start/end is given, returns the node containing line 1.
 * Returns null if no matching node exists or the tree has no nodes.
 */
export function findDocTreeNode(
  treeJson: Record<string, unknown>,
  startLine?: number,
  endLine?: number
): DocTreeNode | null {
  const nodes = (treeJson.nodes ?? []) as DocTreeNode[];
  if (!nodes.length) return null;

  const target = startLine ?? 1;

  function contains(n: DocTreeNode): boolean {
    return n.start_line <= target && n.end_line >= target &&
      (endLine === undefined || n.end_line >= endLine);
  }

  function findDeepest(ns: DocTreeNode[]): DocTreeNode | null {
    let best: DocTreeNode | null = null;
    for (const n of ns) {
      if (contains(n)) {
        const child = findDeepest(n.children ?? []);
        best = child ?? n;
      } else {
        const child = findDeepest(n.children ?? []);
        if (child) best = child;
      }
    }
    return best;
  }

  return findDeepest(nodes);
}

export async function getDocTreeCount(pool: Pool, projectKey: string): Promise<number> {
  const res = await pool.query(
    `SELECT COUNT(*) AS cnt FROM doc_trees WHERE project_key = $1`,
    [projectKey]
  );
  return parseInt(res.rows[0].cnt, 10);
}

export async function getDocTreeNodeTotal(pool: Pool, projectKey: string): Promise<number> {
  const res = await pool.query(
    `SELECT COALESCE(SUM(node_count), 0) AS total FROM doc_trees WHERE project_key = $1`,
    [projectKey]
  );
  return parseInt(res.rows[0].total, 10);
}

export async function getDocTreesMissingCount(pool: Pool, projectKey: string): Promise<number> {
  const res = await pool.query(
    `SELECT COUNT(*) AS cnt FROM doc_trees
     WHERE project_key = $1 AND tree_json::text LIKE '%"summary": null%'`,
    [projectKey]
  );
  return parseInt(res.rows[0].cnt, 10);
}

// ── Simple keyword-based node selection (no external LLM from TS side) ────────

function selectNodesByKeyword(
  nodes: Record<string, unknown>[],
  queryTerms: string[],
  topN: number
): string[] {
  const flat: Array<{ nodeId: string; score: number }> = [];

  function score(n: Record<string, unknown>): number {
    const text = `${n.title} ${n.summary ?? ""}`.toLowerCase();
    let hits = 0;
    for (const t of queryTerms) {
      if (text.includes(t)) hits++;
    }
    return hits;
  }

  function traverse(ns: Record<string, unknown>[]): void {
    for (const n of ns) {
      flat.push({ nodeId: n.node_id as string, score: score(n) });
      traverse((n.children ?? []) as Record<string, unknown>[]);
    }
  }
  traverse(nodes);
  return flat
    .sort((a, b) => b.score - a.score)
    .slice(0, topN)
    .filter((x) => x.score > 0)
    .map((x) => x.nodeId);
}

// ── Public API ────────────────────────────────────────────────────────────────

export async function searchDocTrees(
  pool: Pool,
  projectKey:  string,
  query:       string,
  mode:        "toc_only" | "summary_only" | "node_text",
  tokenBudget: number,
  _workspaceRoot = "/workspace"
): Promise<DocTreeResult[]> {
  const rows = await getAllDocTrees(pool, projectKey);
  if (!rows.length) return [];

  const queryTerms = query
    .toLowerCase()
    .split(/\W+/)
    .filter((t) => t.length >= 3);

  const results: DocTreeResult[] = [];
  let tokensUsed = 0;

  for (const row of rows) {
    if (tokensUsed >= tokenBudget) break;

    const toc = buildToc(row.file_path, row.tree_json);

    if (mode === "toc_only") {
      const tokens = estimateTokens(toc);
      if (tokensUsed + tokens > tokenBudget) continue;
      tokensUsed += tokens;
      results.push({ filePath: row.file_path, toc, relevantNodes: [], estimatedTokens: tokens });
      continue;
    }

    const treeNodes = (row.tree_json.nodes ?? []) as Record<string, unknown>[];
    const selectedIds = selectNodesByKeyword(treeNodes, queryTerms, 5);
    if (!selectedIds.length && mode === "node_text") continue;

    const relevantNodes: DocNode[] = [];
    for (const nid of selectedIds) {
      const n = findNodeById(treeNodes, nid);
      if (!n) continue;
      const entry: DocNode = {
        nodeId:    n.node_id as string,
        title:     n.title as string,
        filePath:  row.file_path,
        startLine: n.start_line as number,
        endLine:   n.end_line as number,
        summary:   (n.summary as string | null) ?? null,
      };
      if (mode === "node_text") {
        entry.text = (n.body_text as string | undefined) ?? "";
      }
      relevantNodes.push(entry);
    }

    const resultTokens = estimateTokens(
      toc + relevantNodes.map((n) => n.text ?? n.summary ?? "").join("\n")
    );
    if (tokensUsed + resultTokens > tokenBudget) continue;
    tokensUsed += resultTokens;

    results.push({ filePath: row.file_path, toc, relevantNodes, estimatedTokens: resultTokens });
  }

  return results;
}

export async function getDocToc(
  pool: Pool,
  projectKey: string,
  filePath:   string
): Promise<string> {
  const row = await getDocTreeByPath(pool, projectKey, filePath);
  if (!row) return "";
  return buildToc(row.file_path, row.tree_json);
}
