/**
 * project_server.ts — AI Context MCP Server (per-project factory)
 *
 * Creates one Express application for a single project. The returned app is NOT
 * started; the caller is responsible for app.listen(project.mcp.port).
 *
 * Project identity is carried by the `project` value object passed to
 * `createProjectServer(...)`. No module-level singletons depend on process.env.
 */

import express, { Request, Response } from "express";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  Tool,
} from "@modelcontextprotocol/sdk/types.js";
import axios from "axios";
import fs from "fs";
import path from "path";

import type { ProjectConfig } from "./config.js";
import { rankedSemanticSearch, LOW_CONFIDENCE_THRESHOLD } from "./context_assembler.js";
import { sliceChunk, describeCoverageGaps, type ChunkPayload, type SlicedPiece } from "./range_slice.js";
import {
  getL0,
  getL1Summaries,
  getRecentChanges,
  getProjectStructure,
  listDirectory,
  getIndexStatus,
  lookupSymbol,
  analyzeImpact,
  getStructuralRoles,
  type StructuralRoleInfo,
} from "./db.js";
import { getChunksByFile, getCollectionInfo } from "./qdrant.js";
import { getDocTreeByPath, findDocTreeNode, getDocToc, getDocTreeCount, getDocTreeNodeTotal, getDocTreesMissingCount } from "./doc_tree.js";
import { sourcesCount, includeExtensions } from "./project_config.js";

// ── Shared services for this project ──────────────────────────────────────────

export interface SharedServices {
  /** Project-scoped PostgreSQL pool. */
  pool: import("pg").Pool;
  /** Qdrant base URL (shared server; collections are project-scoped). */
  qdrantUrl: string;
  /** Shared, mandatory inference service URL — embeddings + re-ranking (see .ai-context/inference/). */
  inferenceUrl: string | null;
  /** How many candidates to re-rank. */
  rerankerTopN: number;
  /** LSP Bridge URL (one shared instance per repo — see docs/lsp-integration.md). */
  lspBridgeUrl: string | null;
}

// ── Logger ────────────────────────────────────────────────────────────────────

type LogLevel = "INFO" | "WARN" | "ERROR";

function makeLog(projectKey: string) {
  return function log(level: LogLevel, message: string, meta?: Record<string, unknown>): void {
    const ts = new Date().toISOString();
    const metaStr = meta && Object.keys(meta).length > 0
      ? "  " + Object.entries(meta).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ")
      : "";
    const line = `${ts}  ${level.padEnd(5)}  [${projectKey}] ${message}${metaStr}`;
    if (level === "ERROR") {
      console.error(line);
    } else if (level === "WARN") {
      console.warn(line);
    } else {
      console.log(line);
    }
  };
}

// ── Tool definitions ──────────────────────────────────────────────────────────

function toolsForEndpoint(endpoint: "mcp" | "admin"): Tool[] {
  const TOOLS: Tool[] = [
    {
      name: "repo_structure",
      description: "",
      inputSchema: {
        type: "object",
        properties: {
          dir_path: { type: "string" },
          depth:    { type: "integer", minimum: 1, maximum: 10 },
        },
      },
    },
    {
      name: "semantic_search",
      description: "",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string" },
        },
        required: ["query"],
      },
    },
    {
      name: "get_file",
      description: "",
      inputSchema: {
        type: "object",
        properties: {
          file_path:  { type: "string" },
          start_line: { type: "integer", minimum: 1 },
          end_line:   { type: "integer", minimum: 1 },
          max_chars:  { type: "integer", minimum: 2000, maximum: 200000 },
        },
        required: ["file_path"],
      },
    },
    {
      name: "analyze_impact",
      description: "",
      inputSchema: {
        type: "object",
        properties: {
          symbol_name:   { type: "string" },
          file_path:     { type: "string" },
          direction:     { type: "string", enum: ["callers", "callees", "both"] },
          depth:         { type: "integer", minimum: 1, maximum: 5, default: 1 },
          include_tests: { type: "boolean", default: true },
        },
        required: ["symbol_name", "direction"],
      },
    },
    {
      name: "resolve_symbol",
      description: "",
      inputSchema: {
        type: "object",
        properties: {
          file_path: { type: "string" },
          line:      { type: "integer", minimum: 1 },
          character: { type: "integer", minimum: 0 },
          kind:      { type: "string", enum: ["definition", "references", "hover"] },
        },
        required: ["file_path", "line", "character", "kind"],
      },
    },
  ];

  const ADMIN_TOOLS: Tool[] = [
    {
      name: "list_recent_changes",
      description: "",
      inputSchema: {
        type: "object",
        properties: {
          limit: { type: "integer", minimum: 1, maximum: 200, default: 10 },
        },
      },
    },
    {
      name: "get_index_status",
      description: "",
      inputSchema: {
        type: "object",
        properties: {},
      },
    },
  ];

  return endpoint === "admin" ? ADMIN_TOOLS : TOOLS;
}

// ── MCP Server factory ────────────────────────────────────────────────────────

function createMcpServer(
  project: ProjectConfig,
  endpoint: "mcp" | "admin",
  instructions: string,
  log: ReturnType<typeof makeLog>,
  services: SharedServices
): Server {
  const server = new Server(
    { name: `ai-context-${project.name}`, version: "2.0.0" },
    { capabilities: { tools: {} }, instructions }
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: toolsForEndpoint(endpoint) }));

  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    const { name, arguments: args } = req.params;
    const start = Date.now();

    const allowedNames = new Set(toolsForEndpoint(endpoint).map((t) => t.name));
    if (!allowedNames.has(name)) {
      log("WARN", `tool_call rejected — not available on this endpoint`, { tool: name, endpoint });
      return {
        isError: true,
        content: [
          {
            type: "text",
            text:
              `Tool '${name}' is not available on this endpoint.\n` +
              `On /mcp: repo_structure, semantic_search, get_file, analyze_impact, resolve_symbol. ` +
              `On /mcp-admin: list_recent_changes, get_index_status.`,
          },
        ],
      };
    }

    const argSummary: Record<string, unknown> = {};
    if (args && typeof args === "object") {
      for (const [k, v] of Object.entries(args as Record<string, unknown>)) {
        argSummary[k] = typeof v === "string" && v.length > 120 ? v.slice(0, 120) + "…" : v;
      }
    }
    log("INFO", `tool_call: ${name}`, argSummary);

    try {
      const result = await dispatch(project, name, args as Record<string, unknown>, services, log);
      log("INFO", `tool_done: ${name}`, { ms: Date.now() - start });
      return result;
    } catch (err) {
      log("ERROR", `tool_error: ${name}`, { ms: Date.now() - start, error: String(err) });
      return {
        isError: true,
        content: [{ type: "text", text: `Tool '${name}' failed: ${String(err)}` }],
      };
    }
  });

  return server;
}

// ── Path normalisation (project-relative) ─────────────────────────────────────

function normalizeAgentPath(input: string, project: ProjectConfig): string {
  const raw = (input ?? "").trim();
  if (!raw) return raw;

  const projectRoot = (project.root_path ?? "").replace(/\\/g, "/");
  const p = raw.replace(/\\/g, "/");

  const stripLeadingSlash = (p2: string) => (p2.startsWith("/") ? p2.slice(1) : p2);

  if (projectRoot) {
    const asRelative = stripLeadingSlash(p);
    if (asRelative === projectRoot) return "";
    if (asRelative.startsWith(projectRoot + "/")) {
      return asRelative.slice(projectRoot.length + 1);
    }

    const idx = p.indexOf("/" + projectRoot + "/");
    if (idx >= 0) {
      return p.slice(idx + projectRoot.length + 2);
    }
    if (p.endsWith("/" + projectRoot)) {
      return "";
    }
  }

  if (p.startsWith("/workspace/")) {
    return p.slice("/workspace/".length);
  }
  if (p === "/workspace") {
    return "";
  }

  return stripLeadingSlash(p);
}

function getExtension(filePath: string): string {
  const base = path.basename(filePath);
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot) : base;
}

// ── LSP bridge language detection ─────────────────────────────────────────────
// Maps a file extension to the language key the LSP bridge's `lsp.enabled_languages`
// config and multilspy's `Language` enum use. Kept in sync manually with
// scripts/validate_config.py:LSP_SUPPORTED_LANGUAGES.
const EXTENSION_TO_LSP_LANGUAGE: Record<string, string> = {
  ".ts": "typescript", ".tsx": "typescript", ".js": "typescript", ".jsx": "typescript",
  ".py": "python",
  ".go": "go",
  ".rs": "rust",
  ".cs": "csharp",
  ".kt": "kotlin", ".kts": "kotlin",
};

function detectLspLanguage(filePath: string): string | null {
  return EXTENSION_TO_LSP_LANGUAGE[getExtension(filePath).toLowerCase()] ?? null;
}

function looksLikeIdentifier(query: string): boolean {
  const q = query.trim();
  return q.length > 0 && !/\s/.test(q) && /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(q);
}

type DirEntry = { file_path: string; language: string; chunk_count: number };

function directoryHint(dirPath: string, children: DirEntry[]) {
  const shown = children.slice(0, 50);
  const listing = shown.map((c) => `- ${c.file_path}  (${c.language}, ${c.chunk_count} chunks)`).join("\n");
  const more = children.length > shown.length ? `\n\n_(showing first ${shown.length} of ${children.length})_` : "";
  return {
    content: [{
      type: "text",
      text: `'${dirPath}' is a directory. Next: call \`repo_structure\` with dir_path="${dirPath}"\n\n## Contents of ${dirPath}/\n${listing}${more}`,
    }],
  };
}

function buildDirectoryTree(
  files: DirEntry[],
  dirPath: string,
  depth: number,
  roles?: Map<string, StructuralRoleInfo>
): string {
  const prefix = dirPath ? (dirPath.endsWith("/") ? dirPath : dirPath + "/") : "";
  const scoped = dirPath ? files.filter((f) => f.file_path.startsWith(prefix)) : files;

  const shown: string[] = [];
  const collapsed = new Map<string, number>();

  for (const f of scoped) {
    const rel = f.file_path.slice(prefix.length);
    const segments = rel.split("/");
    if (segments.length <= depth) {
      const roleInfo = roles?.get(f.file_path);
      const roleTag = roleInfo?.role === "hub"
        ? ` [hub, in-degree ${roleInfo.inDegree}]`
        : roleInfo?.role === "bridge"
        ? ` [bridge]`
        : "";
      shown.push(`${f.file_path}  (${f.language}, ${f.chunk_count} chunks)${roleTag}`);
    } else {
      const collapsedDir = prefix + segments.slice(0, depth).join("/") + "/";
      collapsed.set(collapsedDir, (collapsed.get(collapsedDir) ?? 0) + 1);
    }
  }

  const collapsedLines = [...collapsed.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([dir, count]) => `${dir}…  (${count} more file(s), depth-capped — call again with a deeper dir_path)`);

  return [...shown.sort(), ...collapsedLines].join("\n") || "(empty)";
}

// ── Tool dispatch ─────────────────────────────────────────────────────────────

async function dispatch(
  project: ProjectConfig,
  name: string,
  args: Record<string, unknown>,
  services: SharedServices,
  log: ReturnType<typeof makeLog>
) {
  const key = project.name;
  const { pool, qdrantUrl, inferenceUrl, lspBridgeUrl } = services;

  switch (name) {

    case "repo_structure": {
      const dirPath = normalizeAgentPath((args.dir_path as string) ?? "", project);
      const depth = typeof args.depth === "number" ? Math.max(1, Math.min(10, Math.round(args.depth))) : 2;

      const files = await getProjectStructure(pool, key);
      if (!files.length) {
        return { content: [{ type: "text", text: `No indexed files found. The project may not be indexed yet.` }] };
      }
      const scoped = dirPath ? files.filter((f) => f.file_path.startsWith(dirPath.endsWith("/") ? dirPath : dirPath + "/")) : files;
      if (dirPath && !scoped.length) {
        return { content: [{ type: "text", text: `No indexed files found under '${dirPath}'.` }] };
      }

      const parts: string[] = [];
      if (!dirPath) {
        const l0Text = await getL0(pool, key);
        if (l0Text) parts.push(`## Project Map\n${l0Text}`);
      }
      let roles: Map<string, StructuralRoleInfo> | undefined;
      try {
        roles = await getStructuralRoles(pool, key);
      } catch {
        // import_edges/call_edges may not be populated yet (pre-backfill) — degrade silently, no role tags.
      }
      const tree = buildDirectoryTree(files, dirPath, depth, roles);
      const hasRoleTags = roles && [...roles.values()].some((r) => r.role === "hub" || r.role === "bridge");
      parts.push(`## Directory Tree${dirPath ? `: ${dirPath}/` : ""}  (depth ${depth}, ${scoped.length} file(s))\n${tree}`);
      if (hasRoleTags) {
        parts.push(`_[hub] = highly imported/called (top 10% in-degree); [bridge] = connects both callers and callees; untagged = leaf or ordinary file. Derived from import_edges/call_edges._`);
      }
      log("INFO", `repo_structure result`, { dir_path: dirPath || "(root)", depth, files: scoped.length });
      return { content: [{ type: "text", text: parts.join("\n\n") }] };
    }

    case "semantic_search": {
      const query = ((args.query as string) ?? "").trim();
      if (!query) {
        return { content: [{ type: "text", text: "Missing query." }] };
      }

      if (looksLikeIdentifier(query)) {
        const symbols = await lookupSymbol(pool, key, query, 10);
        if (symbols.length) {
          log("INFO", `semantic_search (symbol fast-path)`, { query, matches: symbols.length });
          const body = symbols
            .map((s) => {
              const parent = s.parent_symbol ? `${s.parent_symbol}.` : "";
              const sig = s.signature ? `\n  ${s.signature}` : "";
              return `- ${s.file_path}:${s.start_line}-${s.end_line}  [${s.symbol_kind}] ${parent}${s.symbol_name}${sig}`;
            })
            .join("\n");
          return {
            content: [{
              type: "text",
              text: `## Symbol matches for "${query}"\n_why: matched identifier '${query}' directly (symbol_index fast-path)_\n\n${body}\n\n_Call \`get_file\` with one of the file paths above to see the source._`,
            }],
          };
        }
      }

      const { hits, l1Summaries, docHits, maxScore } = await rankedSemanticSearch(project, query, 10, pool, inferenceUrl);
      log("INFO", `semantic_search (vector)`, { query, hits: hits.length, doc_hits: docHits.length });

      if (!hits.length && !docHits.length) {
        return { content: [{ type: "text", text: `No results for "${query}". Try a more specific identifier or rephrase.` }] };
      }

      const seenSummary = new Set<string>();
      const hitLines = hits.map((h) => {
        const sym = h.symbol ? ` (${h.symbol})` : "";
        const summary = l1Summaries[h.filePath] && !seenSummary.has(h.filePath)
          ? (seenSummary.add(h.filePath), `\n  L1: ${l1Summaries[h.filePath]}`)
          : "";
        return `- ${h.filePath}:${h.startLine}-${h.endLine}${sym}  [score: ${h.score.toFixed(2)}]\n  why: ${h.why}${summary}`;
      });

      const parts = [`## Search Results: "${query}"\n\n${hitLines.join("\n")}`];
      if (maxScore < LOW_CONFIDENCE_THRESHOLD) {
        parts.unshift(
          `⚠ No strong match (top score ${maxScore.toFixed(2)}) — results below may be irrelevant. ` +
          `Consider rephrasing or checking whether this was removed/renamed.`
        );
      }
      if (docHits.length) {
        parts.push(`## Documentation\n\n${docHits.map((d) => d.toc).join("\n\n")}`);
      }
      parts.push(`_Call \`get_file\` with a file_path above to see the full source or doc section._`);
      return { content: [{ type: "text", text: parts.join("\n\n") }] };
    }

    case "get_file": {
      const filePath = normalizeAgentPath(args.file_path as string, project);
      const startLine = typeof args.start_line === "number" ? args.start_line : undefined;
      const endLine = typeof args.end_line === "number" ? args.end_line : undefined;
      const maxChars = typeof args.max_chars === "number"
        ? Math.max(2000, Math.min(200000, args.max_chars))
        : 50000;

      const ext = getExtension(filePath);
      const allowed = includeExtensions(project);
      if (!allowed.includes(ext)) {
        const children = await listDirectory(pool, key, filePath);
        if (children.length > 0) return directoryHint(filePath, children);
        return {
          content: [{ type: "text", text: `File type '${ext}' is not indexed. Indexed extensions: ${allowed.join(", ")}.` }],
        };
      }

      const docRow = await getDocTreeByPath(pool, key, filePath);
      if (docRow) {
        const node = findDocTreeNode(docRow.tree_json, startLine, endLine);
        let bodyText: string;
        let rangeNote = "";
        if (node && node.body_text) {
          const requestedStart = startLine ?? node.start_line;
          const requestedEnd = endLine ?? node.end_line;
          const piece = sliceChunk(
            { text: node.body_text, start_line: node.start_line, end_line: node.end_line },
            requestedStart,
            requestedEnd
          );
          if (piece) {
            bodyText = piece.lines.join("\n");
            rangeNote = describeCoverageGaps([piece], requestedStart, requestedEnd);
          } else {
            bodyText = "";
            rangeNote = `\n\n_(no indexed content covers lines ${requestedStart}-${requestedEnd} in this file)_`;
          }
        } else {
          bodyText = "";
          const requestedStart = startLine ?? 1;
          const requestedEnd = endLine ?? (node ? node.end_line : requestedStart);
          rangeNote = `\n\n_(no indexed content covers lines ${requestedStart}-${requestedEnd} in this file)_`;
        }
        const toc = await getDocToc(pool, key, filePath);
        let text = `${toc}\n\n---\n\n${bodyText}${rangeNote}`;
        if (text.length > maxChars) {
          text = text.slice(0, maxChars) + "\n\n_(truncated; increase max_chars or pass start_line/end_line to see more)_";
        }
        log("INFO", `get_file (doc-tree)`, { path: filePath, start_line: startLine, end_line: endLine });
        return { content: [{ type: "text", text }] };
      }

      const chunkList = await getChunksByFile(key, filePath, qdrantUrl);
      if (!chunkList.length) {
        const children = await listDirectory(pool, key, filePath);
        if (children.length > 0) return directoryHint(filePath, children);
        const summaries = await getL1Summaries(pool, key, [filePath]);
        if (summaries[filePath]) {
          return { content: [{ type: "text", text: `## ${filePath}\n\n${summaries[filePath]}\n\n_(no chunks found — showing L1 summary only)_` }] };
        }
        return { content: [{ type: "text", text: `No content found for '${filePath}'. The file may not be indexed yet.` }] };
      }

      const sortedChunks = chunkList.slice().sort((a, b) => a.start_line - b.start_line);

      let body: string;
      let rangeNote = "";
      let displayStartLine: number | undefined;
      let displayEndLine: number | undefined;

      if (startLine !== undefined || endLine !== undefined) {
        const requestedStart = startLine ?? sortedChunks[0].start_line;
        const requestedEnd   = endLine   ?? sortedChunks[sortedChunks.length - 1].end_line;

        const overlapping = sortedChunks.filter(
          (c) => c.end_line >= requestedStart && c.start_line <= requestedEnd
        );

        if (overlapping.length > 0) {
          const pieces = overlapping
            .map((c) => sliceChunk(c, requestedStart, requestedEnd))
            .filter((p): p is SlicedPiece => p !== null);
          displayStartLine = pieces[0].start;
          displayEndLine   = pieces[pieces.length - 1].end;
          body = pieces.map((p) => p.lines.join("\n")).join("\n");
          rangeNote = describeCoverageGaps(pieces, requestedStart, requestedEnd);
        } else {
          body = "";
          displayStartLine = requestedStart;
          displayEndLine = requestedEnd;
          rangeNote = `\n\n_(no indexed content covers lines ${requestedStart}-${requestedEnd} in this file)_`;
        }
      } else {
        body = sortedChunks
          .map((c) => {
            const sym = c.symbol ? ` (${c.symbol})` : "";
            return `// Lines ${c.start_line}–${c.end_line}${sym}\n${c.text}`;
          })
          .join("\n\n");
      }

      const lang = sortedChunks[0].language;
      const titleSuffix = displayStartLine !== undefined && displayEndLine !== undefined
        ? `:${displayStartLine}-${displayEndLine}`
        : "";
      let text = `## ${filePath}${titleSuffix}\n\n\`\`\`${lang}\n${body}\n\`\`\`${rangeNote}`;
      if (text.length > maxChars) {
        text = text.slice(0, maxChars) + "\n\n_(truncated; increase max_chars to see more)_";
      }
      log("INFO", `get_file (chunks)`, { path: filePath, chunks: sortedChunks.length });
      return { content: [{ type: "text", text }] };
    }

    case "analyze_impact": {
      const symbolName = ((args.symbol_name as string) ?? "").trim();
      if (!symbolName) {
        return { content: [{ type: "text", text: "Missing symbol_name." }] };
      }
      const rawFilePath = (args.file_path as string) ?? "";
      const filePath = rawFilePath ? normalizeAgentPath(rawFilePath, project) : undefined;
      const direction = (args.direction as string) as "callers" | "callees" | "both";
      if (!["callers", "callees", "both"].includes(direction)) {
        return { content: [{ type: "text", text: `Invalid direction '${direction}' — must be "callers", "callees", or "both".` }] };
      }
      const depth = typeof args.depth === "number" ? Math.max(1, Math.min(5, Math.round(args.depth))) : 1;
      const includeTests = args.include_tests === undefined ? true : Boolean(args.include_tests);

      const result = await analyzeImpact(pool, key, symbolName, filePath, direction, depth, includeTests);
      log("INFO", `analyze_impact result`, {
        symbol: symbolName, direction, depth,
        callers: result.callers.length, callees: result.callees.length,
        ambiguous: result.ambiguous_edges.length, error: (result as any).error,
      });

      if (!result.symbol) {
        return { content: [{ type: "text", text: result.blast_radius_summary }] };
      }

      const parts: string[] = [
        `## Impact Analysis: ${result.symbol.name}  (${result.symbol.file}:${result.symbol.start_line}-${result.symbol.end_line})`,
      ];

      if (direction === "callers" || direction === "both") {
        const body = result.callers.length
          ? result.callers.map((c) => `- ${c.symbol}  (${c.file}:${c.line}, ${c.hops} hop${c.hops > 1 ? "s" : ""})`).join("\n")
          : "(none found)";
        parts.push(`### Callers (${result.callers.length})\n${body}`);
      }
      if (direction === "callees" || direction === "both") {
        const body = result.callees.length
          ? result.callees.map((c) => `- ${c.symbol}  (${c.file}:${c.line}, ${c.hops} hop${c.hops > 1 ? "s" : ""})`).join("\n")
          : "(none found)";
        parts.push(`### Callees (${result.callees.length})\n${body}`);
      }
      if (result.ambiguous_edges.length) {
        const body = result.ambiguous_edges
          .map((e) => `- \`${e.callee_name}\` — could resolve to: ${e.candidates.join(", ") || "(no candidates found)"}`)
          .join("\n");
        parts.push(`### Ambiguous call targets (${result.ambiguous_edges.length})\n${body}`);
      }
      if (includeTests) {
        const body = result.affected_test_files.length
          ? result.affected_test_files.map((f) => `- ${f}`).join("\n")
          : "(none found)";
        parts.push(`### Test files likely covering this symbol\n${body}`);
      }
      parts.push(`**Blast radius:** ${result.blast_radius_summary}`);

      return { content: [{ type: "text", text: parts.join("\n\n") }] };
    }

    case "resolve_symbol": {
      if (!lspBridgeUrl) {
        return { content: [{ type: "text", text: "LSP bridge is not configured for this host." }] };
      }

      const rawFilePath = (args.file_path as string) ?? "";
      if (!rawFilePath) {
        return { content: [{ type: "text", text: "Missing file_path." }] };
      }
      const filePath = normalizeAgentPath(rawFilePath, project);

      const line = args.line as number;
      const character = args.character as number;
      const kind = args.kind as string;
      if (!["definition", "references", "hover"].includes(kind)) {
        return { content: [{ type: "text", text: `Invalid kind '${kind}' — must be "definition", "references", or "hover".` }] };
      }
      if (!Number.isInteger(line) || line < 1) {
        return { content: [{ type: "text", text: "line must be a positive integer (1-based)." }] };
      }
      if (!Number.isInteger(character) || character < 0) {
        return { content: [{ type: "text", text: "character must be a non-negative integer (0-based)." }] };
      }

      const language = detectLspLanguage(filePath);
      if (!language) {
        return {
          content: [{
            type: "text",
            text: `resolve_symbol does not support the file type of '${filePath}'. ` +
              `Supported: ${Object.keys(EXTENSION_TO_LSP_LANGUAGE).join(", ")}.`,
          }],
        };
      }

      const enabledLanguages = project.lsp?.enabled_languages ?? [];
      if (!enabledLanguages.includes(language)) {
        return {
          content: [{
            type: "text",
            text: `Language '${language}' is not enabled for this project (it is auto-detected from the file census — not a config.yaml field). ` +
              `This can happen if the repo's language mix changed since the last resolve, or the language isn't LSP-supported for this repo yet. ` +
              `Re-run 'mise run ai-context-up' to refresh detection; resolve_symbol on ${getExtension(filePath)} files will work once '${language}' is picked up.`,
          }],
        };
      }

      try {
        const resp = await axios.post(
          `${lspBridgeUrl}/resolve`,
          { project_key: key, language, file_path: filePath, line, character, kind },
          { timeout: 30_000 }
        );
        const data = resp.data as {
          kind: string;
          results: Array<{
            file_path: string; start_line: number; start_char: number; end_line: number; end_char: number;
            source_snippet?: string | null;
          }>;
          hover: string | null;
        };

        if (kind === "hover") {
          return { content: [{ type: "text", text: data.hover ?? "(no hover information available)" }] };
        }

        if (!data.results.length) {
          return { content: [{ type: "text", text: `No ${kind} found for ${filePath}:${line}:${character}.` }] };
        }
        // source_snippet is only ever populated for locations OUTSIDE this
        // project's own root_path (i.e. dependency code) — get_file has no
        // access to those (it only reads this project's own indexed
        // content), so the lsp-bridge inlines a snippet read directly off
        // its own disk instead. In-project locations are omitted here
        // (get_file already covers those — call it with the file_path
        // above for full context).
        const body = data.results
          .map((r) => {
            const loc = `- ${r.file_path}:${r.start_line}:${r.start_char}-${r.end_line}:${r.end_char}`;
            if (!r.source_snippet) return loc;
            return `${loc}\n  _(external dependency — get_file can't reach this; snippet below)_\n\`\`\`\n${r.source_snippet}\n\`\`\``;
          })
          .join("\n");
        return { content: [{ type: "text", text: `## ${kind === "definition" ? "Definition" : "References"} (${data.results.length})\n${body}` }] };
      } catch (err) {
        // A 503 with still_starting=true is NOT an error — the lsp-bridge's
        // language-server pool has a bounded wait for cold starts (see
        // lsp-bridge/main.py StillStarting) and asks us to retry instead of
        // hanging until an opaque axios timeout fires. Surface that as an
        // explicit, actionable message so the agent retries later rather than
        // concluding resolve_symbol is broken and giving up on it entirely.
        if (axios.isAxiosError(err) && err.response?.status === 503 && err.response.data?.still_starting) {
          const retryAfter = Math.round(err.response.data.retry_after_seconds ?? 10);
          return {
            content: [{
              type: "text",
              text: `resolve_symbol: the '${language}' language server is still starting up (cold start in progress). ` +
                `This is expected, not a failure — wait ~${retryAfter}s and retry the same call.`,
            }],
          };
        }
        const detail = axios.isAxiosError(err) && err.response
          ? JSON.stringify(err.response.data)
          : String(err);
        return { content: [{ type: "text", text: `resolve_symbol failed: ${detail}` }] };
      }
    }

    case "list_recent_changes": {
      const rawLimit = args.limit as number | undefined;
      const limit = rawLimit === undefined
        ? 10
        : Math.max(1, Math.min(200, Math.round(rawLimit)));
      const changes = await getRecentChanges(pool, key, limit);
      log("INFO", `list_recent_changes result`, { returned: changes.length });
      const text = changes.length
        ? changes.map((c) => `- ${c.file_path}  (${c.chunk_count} chunks, indexed ${new Date(c.indexed_at).toLocaleString()})`).join("\n")
        : "No recently indexed files found.";
      return { content: [{ type: "text", text: `## Recent Changes\n${text}` }] };
    }

    case "get_index_status": {
      const [pgStatus, qdrantInfo, docTreeCount, docTreeNodes, docTreeMissing] = await Promise.all([
        getIndexStatus(pool, key),
        getCollectionInfo(key, qdrantUrl),
        getDocTreeCount(pool, key).catch(() => 0),
        getDocTreeNodeTotal(pool, key).catch(() => 0),
        getDocTreesMissingCount(pool, key).catch(() => 0),
      ]);
      log("INFO", `get_index_status result`, {
        files:     pgStatus.file_count,
        chunks:    pgStatus.chunk_count,
        qdrant:    qdrantInfo.status,
        doc_trees: docTreeCount,
      });
      const statusText =
        `## Index Status: ${key}\n\n` +
        `| Metric | Value |\n|---|---|\n` +
        `| Files indexed         | ${pgStatus.file_count.toLocaleString()} |\n` +
        `| Total L2 chunks       | ${pgStatus.chunk_count.toLocaleString()} |\n` +
        `| L1 summaries current  | ${pgStatus.files_with_l1} / ${pgStatus.file_count} (${pgStatus.file_count > 0 ? Math.round((pgStatus.files_with_l1 / pgStatus.file_count) * 100) : 0}%) |\n` +
        `| Qdrant vectors        | ${qdrantInfo.vector_count === pgStatus.chunk_count
          ? "✓ matches PostgreSQL"
          : qdrantInfo.vector_count === -1
            ? "⚠ unreachable"
            : `⚠ ${qdrantInfo.vector_count} vectors (PostgreSQL has ${pgStatus.chunk_count} chunks)`} |\n` +
        `| Qdrant collection     | ${qdrantInfo.status} |\n` +
        `| Last indexed          | ${pgStatus.newest_indexed_at ?? "—"} |\n` +
        `| PostgreSQL            | connected |\n` +
        `| Doc trees             | ${docTreeCount} files |\n` +
        `| Doc tree nodes total  | ${docTreeNodes} |\n` +
        `| Doc trees missing summaries | ${docTreeMissing} |`;

      return { content: [{ type: "text", text: statusText }] };
    }

    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

// ── Instructions loading ──────────────────────────────────────────────────────

function loadInstructions(project: ProjectConfig): string {
  const candidates = [
    process.env.INSTRUCTIONS_PATH ?? "",
    "/app/instructions.md",
    "/app/.ai-context/instructions.md",
  ].filter(Boolean);

  for (const candidate of candidates) {
    try {
      const raw = fs.readFileSync(candidate, "utf8");
      return raw
        .replace(/{project_name}/g, project.name)
        .replace(/{display_name}/g, project.display_name);
    } catch {
      // file not found — try next
    }
  }

  return [
    `# ${project.display_name} — AI Context Instructions`,
    "",
    `This MCP server provides code context for the **${project.display_name}** project (key: \`${project.name}\`).`,
    "",
    "See the repo-wide skill `ai-context-workflow` " +
    "(generated at `.ai-context/skills/ai-context-workflow/SKILL.md`) for the full workflow.",
    "",
    "## Tools",
    "",
    "1. `repo_structure` — call first for orientation (module map, directory tree).",
    "   Per-file entries may show [hub]/[bridge] structural-role tags derived",
    "   from the import/call graph.",
    "2. `semantic_search(query)` — use technical identifiers, not prose, where possible.",
    "   Each hit includes a `why` note (identifier match, vector similarity,",
    "   keyword boost, or rerank) — use it to judge trustworthiness.",
    "3. `get_file(file_path, [start_line], [end_line])` — only if step 2 wasn't enough.",
    "   Works for code and docs automatically. Max ~2 calls per query, max 3 files.",
    "4. `analyze_impact(symbol_name, direction, [file_path], [depth], [include_tests])` —",
    "   before editing/removing a symbol, to see its callers/callees (blast radius)",
    "   and likely-covering test files.",
    "",
    "Stop as soon as you have enough context — don't call every tool by default.",
  ].join("\n");
}

// ── Project app factory ───────────────────────────────────────────────────────

export function createProjectServer(
  project: ProjectConfig,
  services: SharedServices
): express.Express {
  const log = makeLog(project.name);
  const instructions = loadInstructions(project);
  const app = express();

  app.get("/health", (_req: Request, res: Response) => {
    res.json({
      status:           "ok",
      project_key:      project.name,
      sources_resolved: sourcesCount(project),
    });
  });

  app.get("/instructions", (_req: Request, res: Response) => {
    const text = loadInstructions(project);
    res.setHeader("Content-Type", "text/markdown; charset=utf-8");
    res.send(text);
  });

  app.all("/search", express.json(), async (req: Request, res: Response) => {
    const query: string =
      (req.method === "GET" ? req.query.q : (req.body as Record<string, unknown>)?.query) as string;

    if (!query || typeof query !== "string" || !query.trim()) {
      res.status(400).send("Missing query. Use GET /search?q=<query> or POST /search {query:\"…\"}");
      return;
    }

    const trimmed = query.trim();
    log("INFO", `search (REST): ${trimmed.slice(0, 120)}`);
    const start = Date.now();

    try {
      const result = await rankedSemanticSearch(project, trimmed, 10, services.pool, services.inferenceUrl);
      log("INFO", `search (REST) done`, {
        ms:      Date.now() - start,
        chunks:  result.hits.length,
      });
      res.setHeader("Content-Type", "text/plain; charset=utf-8");
      res.setHeader("X-Chunk-Count", String(result.hits.length));
      res.send(
        `## Search Results: "${trimmed}"\n\n` +
        result.hits.map((h) => `- ${h.filePath}:${h.startLine}-${h.endLine}  [score: ${h.score.toFixed(2)}]`).join("\n")
      );
    } catch (err) {
      log("ERROR", `search (REST) failed`, { error: String(err) });
      res.status(500).send(`Search failed: ${String(err)}`);
    }
  });

  app.all("/mcp", express.json(), async (req: Request, res: Response) => {
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    const server = createMcpServer(project, "mcp", instructions, log, services);
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
      res.on("finish", () => server.close().catch(() => {}));
    } catch (err) {
      log("ERROR", "MCP request error", { error: String(err) });
      if (!res.headersSent) {
        res.status(500).json({ error: String(err) });
      }
    }
  });

  app.all("/mcp-admin", express.json(), async (req: Request, res: Response) => {
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    const server = createMcpServer(project, "admin", instructions, log, services);
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
      res.on("finish", () => server.close().catch(() => {}));
    } catch (err) {
      log("ERROR", "MCP-admin request error", { error: String(err) });
      if (!res.headersSent) {
        res.status(500).json({ error: String(err) });
      }
    }
  });

  return app;
}
