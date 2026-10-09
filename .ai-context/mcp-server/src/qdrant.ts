import axios from "axios";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface QdrantHit {
  score: number;
  payload: {
    project_key:   string;
    relative_path: string;
    language:      string;
    text:          string;
    start_line:    number;
    end_line:      number;
    symbol:        string | null;
    chunk_type:    string;
  };
  /** Optional provenance note ("why" this hit scored the way it did) — set
   * during reranking (see keywordRerank/crossEncoderRerank in
   * context_assembler.ts), not part of the raw Qdrant response. */
  why?: string;
}

// ── Semantic search ───────────────────────────────────────────────────────────

/**
 * Search a Qdrant collection.
 * Every search filters by project_key to prevent cross-contamination.
 * When pathPrefix is set we over-fetch (topK*4) and filter client-side so
 * the final set stays within scope without requiring a payload index change.
 */
export async function searchChunks(
  projectKey:      string,
  vector:          number[],
  topK:            number,
  qdrantBaseUrl:   string,
  scoreThreshold = 0.35,
  pathPrefix     = ""
): Promise<QdrantHit[]> {
  const fetchLimit = pathPrefix ? topK * 4 : topK;

  const resp = await axios.post(
    `${qdrantBaseUrl}/collections/${projectKey}/points/search`,
    {
      vector,
      limit:           fetchLimit,
      with_payload:    true,
      score_threshold: scoreThreshold,
      filter: {
        must: [{ key: "project_key", match: { value: projectKey } }],
      },
    },
    { timeout: 15000 }
  );

  let hits = (resp.data.result ?? []) as QdrantHit[];

  if (pathPrefix) {
    const prefix = pathPrefix.endsWith("/") ? pathPrefix : pathPrefix + "/";
    hits = hits.filter((h) => h.payload.relative_path.startsWith(prefix));
  }

  return hits.slice(0, topK);
}

/**
 * Cross-project search helper — searches another project's collection.
 * Depth is capped at 1 by the caller (context_assembler.ts).
 */
export async function searchOther(
  sourceKey:       string,
  vector:          number[],
  topK:            number,
  qdrantBaseUrl:   string,
  scoreThreshold = 0.35,
  scope          = ""
): Promise<QdrantHit[]> {
  return searchChunks(sourceKey, vector, topK, qdrantBaseUrl, scoreThreshold, scope);
}

// ── Collection health ─────────────────────────────────────────────────────────

/**
 * Returns the Qdrant collection's vector count and status string.
 * Never throws — returns status "unreachable" if Qdrant is down so the
 * caller can include it in a status report without crashing.
 */
export async function getCollectionInfo(
  projectKey: string,
  qdrantBaseUrl: string
): Promise<{ vector_count: number; status: string }> {
  try {
    const resp = await axios.get(
      `${qdrantBaseUrl}/collections/${projectKey}`,
      { timeout: 5000 }
    );
    const info = resp.data.result ?? {};
    return {
      vector_count: info.vectors_count ?? 0,
      status:       info.status        ?? "unknown",
    };
  } catch {
    return { vector_count: -1, status: "unreachable" };
  }
}

// ── Scroll by file ────────────────────────────────────────────────────────────

/**
 * Returns every chunk for one file, paginating through Qdrant's `scroll`
 * API via `next_page_offset` until exhausted. A single page is capped at
 * 256 points server-side — files with more chunks than that (large/finely
 * chunked files, e.g. YAML with many top-level keys) used to be silently
 * truncated to just the first page (in undefined/ID order, not line order),
 * which could make `get_file`'s start_line/end_line filtering appear to
 * "not work" simply because the requested chunk was never fetched at all.
 */
export async function getChunksByFile(
  projectKey:   string,
  relativePath: string,
  qdrantBaseUrl: string
): Promise<QdrantHit["payload"][]> {
  const pageSize = 256;
  const payloads: QdrantHit["payload"][] = [];
  let offset: unknown = undefined;

  for (;;) {
    const resp = await axios.post(
      `${qdrantBaseUrl}/collections/${projectKey}/points/scroll`,
      {
        filter: {
          must: [
            { key: "project_key",   match: { value: projectKey } },
            { key: "relative_path", match: { value: relativePath } },
          ],
        },
        limit:        pageSize,
        offset,
        with_payload: true,
        with_vector:  false,
      },
      { timeout: 15000 }
    );

    const points: Array<{ payload: QdrantHit["payload"] }> = resp.data.result?.points ?? [];
    payloads.push(...points.map((p) => p.payload));

    const nextOffset = resp.data.result?.next_page_offset ?? null;
    if (!nextOffset || points.length === 0) break;
    offset = nextOffset;
  }

  return payloads.sort((a, b) => a.start_line - b.start_line);
}
