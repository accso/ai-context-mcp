/**
 * context_assembler.ts
 *
 * Two exported entry points, but only one is on a live serving path:
 *
 *   1. rankedSemanticSearch(project, query, k) — backs BOTH the `semantic_search`
 *      MCP tool and the `/search` REST route (project_server.ts). Returns raw
 *      ranked chunk hits + L1 summaries, no LLM, no prose condensation, no
 *      cross-project source expansion. The calling agent decides what's relevant.
 *   2. assembleContext(project, query) — NOT currently called from
 *      project_server.ts (no live route uses it). Assembles a full L0+L1+L2
 *      text blob within a token budget, with cross-project `sources`
 *      expansion. Kept as a reusable building block; re-wire it to a route
 *      explicitly if this assembled-context behavior is wanted again.
 *
 * Assembly order (per token budget) for assembleContext:
 *   own L0 → source L0s → own L2 chunks → own L1 summaries
 *   → source L1 summaries → source L2 chunks
 *
 * Cross-encoder/ColBERT re-ranking (mandatory, always-on inference service):
 *   After multi-query Qdrant search, send top candidates to the shared
 *   inference service's /rerank endpoint for score calibration. Falls back
 *   to the keyword boost only on a transient failure (network error,
 *   service briefly restarting) — not a user-facing config toggle anymore,
 *   see docs/architecture-deep-dive.md.
 *
 * Cross-project source routing (per source entry, assembleContext only —
 * not reachable from rankedSemanticSearch/`/search`/`semantic_search` today):
 *   1. If `when` keywords contain any query word → include.
 *   2. Else: semantic search; include if any hit exceeds threshold (0.55).
 *
 * Source depth cap: 1.  Sources-of-sources are never queried.
 */
import axios from "axios";
import { Pool } from "pg";
import type { ProjectConfig, ProfileInfo, Source } from "./config.js";
import { embedQuery } from "./embedder.js";
import { searchChunks, searchOther, getChunksByFile, QdrantHit } from "./qdrant.js";
import { getL0, getL1Summaries, getSourcesL0, getSourceL1Summaries } from "./db.js";
import { projectKey, maxTokens, sources, profile, rerankerTopN } from "./project_config.js";
import { searchDocTrees } from "./doc_tree.js";

const CHARS_PER_TOKEN         = 4;
const SOURCE_SCORE_THRESHOLD  = 0.55;
const KEYWORD_BOOST_WEIGHT    = 0.25;
export const LOW_CONFIDENCE_THRESHOLD = 0.5;

export function estimateTokens(text: string): number {
  return Math.ceil(text.length / CHARS_PER_TOKEN);
}

async function crossEncoderRerank(
  query:    string,
  hits:     QdrantHit[],
  topN:     number,
  inferenceUrl: string | null
): Promise<QdrantHit[]> {
  const url = inferenceUrl;
  if (!url || !hits.length) return hits;

  try {
    const candidates = hits.map((h) => ({
      id:    `${h.payload.relative_path}:${h.payload.start_line}`,
      text:  h.payload.text,
      score: h.score,
    }));

    const resp = await axios.post(
      `${url}/rerank`,
      { query, candidates, top_n: Math.max(topN, hits.length) },
      { timeout: 10_000 }
    );

    const ranked = resp.data as Array<{ id: string; score: number; original_score: number }>;
    const scoreMap = new Map(ranked.map((r) => [r.id, r.score]));

    return hits
      .map((h) => {
        const id = `${h.payload.relative_path}:${h.payload.start_line}`;
        const rerankedScore = scoreMap.get(id);
        const preRerankScore = h.score;
        return {
          ...h,
          score: rerankedScore ?? preRerankScore,
          why: rerankedScore !== undefined
            ? `cross-encoder rerank score ${rerankedScore.toFixed(2)} (pre-rank vector similarity ${preRerankScore.toFixed(2)})`
            : `vector similarity ${preRerankScore.toFixed(2)} (not reranked)`,
        };
      })
      .sort((a, b) => b.score - a.score);
  } catch {
    return keywordRerank(query, hits);
  }
}

export function extractTerms(query: string): string[] {
  const raw = query.split(/\W+/).filter(Boolean);
  const expanded: string[] = [];
  for (const token of raw) {
    expanded.push(token.toLowerCase());
    const parts = token.split(/(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])/);
    if (parts.length > 1) expanded.push(...parts.map((p) => p.toLowerCase()));
  }
  return [...new Set(expanded)].filter((t) => t.length >= 3);
}

export function keywordScore(queryTerms: string[], text: string): number {
  if (!queryTerms.length) return 0;
  const lower = text.toLowerCase();
  let hits = 0;
  for (const term of queryTerms) {
    let pos = 0;
    while ((pos = lower.indexOf(term, pos)) !== -1) {
      hits++;
      pos += term.length;
    }
  }
  return Math.min(hits / 10, 1.0);
}

export function keywordRerank(originalQuery: string, hits: QdrantHit[]): QdrantHit[] {
  const terms = extractTerms(originalQuery);
  if (!terms.length) {
    return hits.map((hit) => ({ ...hit, why: `vector similarity ${hit.score.toFixed(2)}` }));
  }
  return hits
    .map((hit) => {
      const preRerankScore = hit.score;
      const boost = KEYWORD_BOOST_WEIGHT * keywordScore(terms, hit.payload.text);
      return {
        ...hit,
        score: (1 - KEYWORD_BOOST_WEIGHT) * preRerankScore + boost,
        why: `vector similarity ${preRerankScore.toFixed(2)}, keyword boost +${boost.toFixed(2)}`,
      };
    })
    .sort((a, b) => b.score - a.score);
}

// ── Documentation query heuristic ────────────────────────────────────────────

const DOC_KEYWORDS = [
  "how", "what", "why", "when", "where", "which",
  "architecture", "design", "overview", "explain", "describe",
  "document", "guide", "tutorial", "reference", "adr", "rfc",
  "wiki", "decision", "concept", "pattern", "structure",
];

export function isDocumentationQuery(query: string): boolean {
  const lower = query.toLowerCase();
  return DOC_KEYWORDS.some((kw) => lower.includes(kw));
}

export function topQdrantHitsIncludeDocPaths(hits: QdrantHit[]): boolean {
  const DOC_PATH_PATTERNS = [/\.md$/i, /docs?\//i, /wiki\//i, /adr\//i, /architecture\//i, /rfc\//i];
  return hits.slice(0, 5).some((h) =>
    DOC_PATH_PATTERNS.some((re) => re.test(h.payload.relative_path))
  );
}

// ── Query expansion ────────────────────────────────────────────────────────────

const MAX_QUERY_VARIANTS = 4;

const GENERIC_VERB_SYNONYM_GROUPS: string[][] = [
  ["decide", "determine", "compute"],
  ["configure", "set up"],
  ["route", "dispatch"],
  ["write", "persist", "store"],
];

export function synonymSubstitutionVariant(query: string): string | null {
  for (const group of GENERIC_VERB_SYNONYM_GROUPS) {
    for (const word of group) {
      const re = new RegExp(`\\b${word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i");
      const match = re.exec(query);
      if (!match) continue;
      const replacement = group.find((w) => w.toLowerCase() !== word.toLowerCase());
      if (!replacement) continue;
      return query.slice(0, match.index) + replacement + query.slice(match.index + match[0].length);
    }
  }
  return null;
}

export function reorderingVariants(query: string): string[] {
  const allVerbs = GENERIC_VERB_SYNONYM_GROUPS.flat().join("|");
  const re = new RegExp(`^how (?:does|do|is|are)\\s+(.+?)\\s+(?:${allVerbs})\\w*\\s+(.+?)\\??$`, "i");
  const match = re.exec(query.trim());
  if (!match) return [];
  const [, x, y] = match;
  return [`${y} ${x}`, `${x} ${y} implementation`];
}

export function genericParaphraseVariants(query: string): string[] {
  const variants: string[] = [];
  const synonymVariant = synonymSubstitutionVariant(query);
  if (synonymVariant) variants.push(synonymVariant);
  variants.push(...reorderingVariants(query));
  return variants;
}

export function expandQueries(query: string, proj_profile: ProfileInfo): string[] {
  const queries = [query];
  const components  = proj_profile.components ?? [];
  const hasKotlin   = components.some(c => c.language === "kotlin");
  const hasJava     = components.some(c => c.language === "java");
  const hasReact    = components.some(c => c.framework === "react");
  const hasAngular  = components.some(c => c.framework === "angular");
  const hasVue      = components.some(c => c.framework === "vue");
  const hasFrontend = components.some(c => c.layer === "frontend") || hasReact || hasAngular || hasVue;

  if (hasKotlin) {
    queries.push(`${query} Koin module dependency injection`);
    if (/endpoint|route|api|GET|POST|PUT|DELETE|REST|controller|path/i.test(query)) {
      queries.push(`${query} Ktor routing route`);
    }
  }
  if (hasJava) {
    queries.push(`${query} Spring Bean dependency injection`);
    if (/endpoint|route|api|GET|POST|PUT|DELETE|REST|controller|mapping/i.test(query)) {
      queries.push(`${query} Spring RestController RequestMapping`);
    }
    if (/persist|repository|entity|jpa|database|query/i.test(query)) {
      queries.push(`${query} JPA Repository Entity`);
    }
  }
  if (hasFrontend) {
    if (/component|page|view|screen/i.test(query))  queries.push(`${query} React component hooks`);
    if (/service|api|http|fetch/i.test(query))       queries.push(`${query} Angular service injectable HTTP`);
  }
  if (hasVue) {
    if (/component|page|view|screen/i.test(query))  queries.push(`${query} Vue component Options Composition API`);
    if (/store|state|pinia|vuex/i.test(query))       queries.push(`${query} Pinia store defineStore`);
    if (/router|route|navigation/i.test(query))      queries.push(`${query} Vue Router useRouter`);
    if (/service|api|http|fetch/i.test(query))       queries.push(`${query} Vue composable useHttp fetch`);
  }

  for (const variant of genericParaphraseVariants(query)) {
    if (queries.length >= MAX_QUERY_VARIANTS) break;
    if (!queries.includes(variant)) queries.push(variant);
  }

  return queries.slice(0, MAX_QUERY_VARIANTS);
}

// ── Multi-query search + dedup ────────────────────────────────────────────────

interface MultiQuerySearchResult {
  hits:              QdrantHit[];
  preRerankMaxScore: number;
}

async function multiQuerySearch(
  queries:       string[],
  originalQuery: string,
  project:       ProjectConfig,
  pool:          Pool,
  k:             number,
  pathPrefix:    string,
  inferenceUrl:  string | null,
): Promise<MultiQuerySearchResult> {
  const fetchK = k * 3;
  const seen = new Map<string, QdrantHit>();
  const qdrantBaseUrl = deriveQdrantUrl();

  for (const q of queries) {
    const vec  = await embedQuery(q, inferenceUrl);
    const hits = await searchChunks(project.name, vec, fetchK, qdrantBaseUrl, 0.35, pathPrefix);
    for (const hit of hits) {
      const key      = `${hit.payload.relative_path}:${hit.payload.start_line}`;
      const existing = seen.get(key);
      if (!existing || hit.score > existing.score) seen.set(key, hit);
    }
  }
  const allHits           = [...seen.values()].sort((a, b) => b.score - a.score);
  const preRerankMaxScore = allHits[0]?.score ?? 0;

  const topN = rerankerTopN();
  const hits = inferenceUrl
    ? await crossEncoderRerank(originalQuery, allHits, topN, inferenceUrl)
    : await keywordRerank(originalQuery, allHits);
  return { hits, preRerankMaxScore };
}

function deriveQdrantUrl(): string {
  return process.env.QDRANT_URL ?? "http://localhost:6333";
}

// ── Ranked semantic search ────────────────────────────────────────────────────

export interface RankedHit {
  filePath:  string;
  startLine: number;
  endLine:   number;
  score:     number;
  symbol:    string | null;
  language:  string;
  /** Provenance note: why this hit scored/ranked the way it did (§1.5). */
  why:       string;
}

export interface DocHit {
  filePath: string;
  toc:      string;
}

export interface RankedSearchResult {
  hits:        RankedHit[];
  l1Summaries: Record<string, string>;
  docHits:     DocHit[];
  maxScore:    number;
}

export async function rankedSemanticSearch(
  project: ProjectConfig,
  query: string,
  k: number,
  pool: Pool,
  inferenceUrl: string | null = null
): Promise<RankedSearchResult> {
  const pkey    = projectKey(project);
  const subQ    = expandQueries(query, profile(project));
  const { hits: allHits, preRerankMaxScore } = await multiQuerySearch(subQ, query, project, pool, k, "", inferenceUrl);
  const top     = allHits.slice(0, k);

  const hits: RankedHit[] = top.map((h) => ({
    filePath:  h.payload.relative_path,
    startLine: h.payload.start_line,
    endLine:   h.payload.end_line,
    score:     h.score,
    symbol:    h.payload.symbol,
    language:  h.payload.language,
    why:       h.why ?? `vector similarity ${h.score.toFixed(2)}`,
  }));

  const paths       = [...new Set(hits.map((h) => h.filePath))];
  const l1Summaries = await getL1Summaries(pool, pkey, paths);

  let docHits: DocHit[] = [];
  if (isDocumentationQuery(query) || topQdrantHitsIncludeDocPaths(allHits)) {
    try {
      const tocResults = await searchDocTrees(pool, pkey, query, "toc_only", 1500);
      docHits = tocResults.map((d) => ({ filePath: d.filePath, toc: d.toc }));
    } catch {
      // Doc trees unavailable — skip silently
    }
  }

  return { hits, l1Summaries, docHits, maxScore: preRerankMaxScore };
}

// ── File-anchored result grouping ─────────────────────────────────────────────

export interface FileGroup {
  relativePath: string;
  maxScore:     number;
  topChunks:    QdrantHit[];
}

export function groupByFile(hits: QdrantHit[]): FileGroup[] {
  const fileMap = new Map<string, FileGroup>();
  for (const hit of hits) {
    const fp = hit.payload.relative_path;
    const existing = fileMap.get(fp);
    if (existing) {
      existing.topChunks.push(hit);
      if (hit.score > existing.maxScore) existing.maxScore = hit.score;
    } else {
      fileMap.set(fp, { relativePath: fp, maxScore: hit.score, topChunks: [hit] });
    }
  }
  return [...fileMap.values()].sort((a, b) => b.maxScore - a.maxScore);
}

// ── Source routing decision ────────────────────────────────────────────────────

export function matchesWhenKeywords(query: string, when: string): boolean {
  if (!when) return false;
  return when.split(",").map((k) => k.trim()).filter(Boolean).some((kw) =>
    new RegExp(`\\b${kw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i").test(query)
  );
}

async function shouldIncludeSource(
  query:     string,
  queryVec:  number[],
  src:       Source,
  k:         number,
): Promise<boolean> {
  if (matchesWhenKeywords(query, src.when)) return true;

  try {
    const hits = await searchOther(src.project, queryVec, k, deriveQdrantUrl(), SOURCE_SCORE_THRESHOLD, src.scope);
    return hits.length > 0;
  } catch {
    return false;
  }
}

// ── Public assembler ──────────────────────────────────────────────────────────

export interface AssembledContext {
  text:             string;
  sources_used:     string[];
  chunk_count:      number;
  estimated_tokens: number;
}

export async function assembleContext(
  project: ProjectConfig,
  query: string,
  pool: Pool,
  inferenceUrl: string | null = null
): Promise<AssembledContext> {
  const pkey        = projectKey(project);
  const k           = 8;
  const nFiles      = 3;
  const fileChunCap = 30;
  const budget      = maxTokens(project);
  const cfgSources  = sources(project);
  const projProfile = profile(project);

  const parts:       string[] = [];
  const sourcesUsed: string[] = [pkey];
  let   usedTokens = 0;

  function tryAdd(text: string): boolean {
    if (usedTokens + estimateTokens(text) >= budget) return false;
    parts.push(text);
    usedTokens += estimateTokens(text);
    return true;
  }

  const l0Text = await getL0(pool, pkey);
  if (l0Text) tryAdd(`## Project Map\n${l0Text}`);

  const subQueries  = expandQueries(query, projProfile);
  const queryVec    = await embedQuery(query, inferenceUrl);
  const { hits: allHits } = await multiQuerySearch(subQueries, query, project, pool, k, "", inferenceUrl);
  const fileGroups  = groupByFile(allHits);

  const topFileGroups  = fileGroups.slice(0, nFiles);
  const restHits       = fileGroups.slice(nFiles).flatMap((g) => g.topChunks)
                           .sort((a, b) => b.score - a.score)
                           .slice(0, k);

  const fullFileChunks = new Map<string, QdrantHit["payload"][]>();
  for (const fg of topFileGroups) {
    const chunks = await getChunksByFile(pkey, fg.relativePath, deriveQdrantUrl());
    fullFileChunks.set(fg.relativePath, chunks.slice(0, fileChunCap));
  }

  const hitPaths = [
    ...topFileGroups.map((g) => g.relativePath),
    ...new Set(restHits.map((h) => h.payload.relative_path)),
  ];

  const relevantSources: Array<{ src: Source; hits: QdrantHit[] }> = [];
  for (const src of cfgSources) {
    if (await shouldIncludeSource(query, queryVec, src, Math.ceil(k / 2))) {
      const srcHits = src.mode !== "l0"
        ? await searchOther(src.project, queryVec, Math.ceil(k / 2), deriveQdrantUrl(), 0.35, src.scope)
        : [];
      relevantSources.push({ src, hits: srcHits });
    }
  }

  if (relevantSources.length > 0) {
    const srcL0Map = await getSourcesL0(pool, relevantSources.map((r) => r.src.project));
    for (const { src } of relevantSources) {
      if (usedTokens >= budget) break;
      if (srcL0Map[src.project]) {
        tryAdd(`## Source Project: ${src.project}\n### Project Map\n${srcL0Map[src.project]}`);
      }
    }
  }

  const needsDocContext = isDocumentationQuery(query) || topQdrantHitsIncludeDocPaths(allHits);
  if (needsDocContext && usedTokens < budget) {
    try {
      const tocBudget = Math.min(budget - usedTokens, 1200);
      const tocResults = await searchDocTrees(pool, pkey, query, "toc_only", tocBudget);
      if (tocResults.length > 0) {
        tryAdd("## Documentation Structure");
        for (const dr of tocResults) {
          if (usedTokens >= budget) break;
          tryAdd(`### ${dr.filePath}\n${dr.toc}`);
        }
      }
    } catch {
      // skip silently
    }
  }

  if (topFileGroups.length > 0) {
    tryAdd("## Relevant Code");
    for (const fg of topFileGroups) {
      if (usedTokens >= budget) break;
      const chunks = fullFileChunks.get(fg.relativePath) ?? [];
      if (!chunks.length) continue;
      const lang = chunks[0].language;
      const body = chunks
        .map((c) => {
          const sym = c.symbol ? ` — ${c.symbol}` : "";
          return `// Lines ${c.start_line}–${c.end_line}${sym}\n${c.text}`;
        })
        .join("\n\n");
      tryAdd(`### ${fg.relativePath}  [best score: ${fg.maxScore.toFixed(2)}]\n\`\`\`${lang}\n${body}\n\`\`\``);
    }
  }

  for (const h of restHits) {
    if (usedTokens >= budget) break;
    const loc = `${h.payload.relative_path}:${h.payload.start_line}-${h.payload.end_line}`;
    const sym = h.payload.symbol ? ` (${h.payload.symbol})` : "";
    tryAdd(`### ${loc}${sym}  [score: ${h.score.toFixed(2)}]\n\`\`\`${h.payload.language}\n${h.payload.text}\n\`\`\``);
  }

  if (needsDocContext && usedTokens < budget) {
    try {
      const nodesBudget = Math.min(budget - usedTokens, 2000);
      const nodeResults = await searchDocTrees(pool, pkey, query, "node_text", nodesBudget);
      if (nodeResults.length > 0) {
        tryAdd("## Documentation Sections");
        for (const dr of nodeResults) {
          if (usedTokens >= budget) break;
          for (const node of dr.relevantNodes) {
            if (usedTokens >= budget) break;
            const nodeText = node.text ?? node.summary ?? "";
            if (!nodeText) continue;
            tryAdd(
              `### ${dr.filePath} § ${node.title}  (lines ${node.startLine}–${node.endLine})\n${nodeText}`
            );
          }
        }
      }
    } catch {
      // skip silently
    }
  }

  const l1Map = await getL1Summaries(pool, pkey, hitPaths);
  if (Object.keys(l1Map).length > 0) {
    tryAdd("## File Summaries");
    for (const [fp, summary] of Object.entries(l1Map)) {
      if (usedTokens >= budget) break;
      tryAdd(`### ${fp}\n${summary}`);
    }
  }

  for (const { src, hits: srcHits } of relevantSources) {
    if (usedTokens >= budget) break;
    sourcesUsed.push(src.project);

    if (src.mode === "l0+l1" || src.mode === "l0+l1+l2") {
      const srcPaths = [...new Set(srcHits.map((h) => h.payload.relative_path))];
      const srcL1Map = await getSourceL1Summaries(pool, src.project, srcPaths, src.scope || undefined);
      for (const [fp, s] of Object.entries(srcL1Map)) {
        if (usedTokens >= budget) break;
        tryAdd(`#### ${src.project}/${fp}\n${s}`);
      }
    }

    if (src.mode === "l0+l1+l2") {
      for (const h of srcHits) {
        if (usedTokens >= budget) break;
        const loc = `${src.project}/${h.payload.relative_path}:${h.payload.start_line}-${h.payload.end_line}`;
        tryAdd(`#### ${loc}  [score: ${h.score.toFixed(2)}]\n\`\`\`${h.payload.language}\n${h.payload.text}\n\`\`\``);
      }
    }
  }

  const allChunkCount = topFileGroups.reduce((s, g) => s + (fullFileChunks.get(g.relativePath)?.length ?? 0), 0)
                      + restHits.length;
  const finalText = parts.join("\n\n");
  return {
    text:             finalText,
    sources_used:     [...new Set(sourcesUsed)],
    chunk_count:      allChunkCount,
    estimated_tokens: estimateTokens(finalText),
  };
}
