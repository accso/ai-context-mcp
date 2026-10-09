/**
 * fake-inference-server.ts — tiny stub for the real inference service (see
 * .ai-context/inference/main.py), implementing both endpoints
 * embedder.ts/context_assembler.ts actually call:
 *
 *   POST /embeddings -> {data:[{embedding,index}]}
 *   POST /rerank      -> [{id,score,original_score}]
 *
 * Uses the SAME deterministic, content-derived vector formula as
 * tests/e2e/index_fixture_repo.py's fake_embed() — this file and that one
 * must be kept in sync, or query-time and index-time vectors stop being
 * comparable. Real embedding/re-ranking quality is explicitly out of scope
 * for this E2E suite (same stance as the Tier 2 integration tests) — this
 * only proves the real HTTP call plumbing (embedQuery/crossEncoderRerank ->
 * this service -> Qdrant search) works, end to end.
 *
 * /rerank is a deliberate no-op (echoes each candidate's original score
 * back, unsorted) rather than a real scoring model — good enough to
 * exercise the real request/response code path in context_assembler.ts
 * without needing a real ColBERT/cross-encoder model in this suite.
 */
import http from "http";
import type { AddressInfo } from "net";

export const FAKE_VECTOR_DIM = 8;

export function fakeEmbed(text: string, dim: number = FAKE_VECTOR_DIM): number[] {
  let total = 0;
  for (let i = 0; i < text.length; i++) total += text.charCodeAt(i);
  return Array.from({ length: dim }, (_, i) => ((total * (i + 1)) % 1000) / 1000);
}

export interface FakeInferenceServer {
  url: string;
  close: () => Promise<void>;
}

export async function startFakeInferenceServer(): Promise<FakeInferenceServer> {
  const server = http.createServer((req, res) => {
    if (req.method !== "POST") {
      res.writeHead(404).end();
      return;
    }
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      try {
        if (req.url?.endsWith("/embeddings")) {
          const parsed = JSON.parse(body) as { input: string | string[] };
          const inputs = Array.isArray(parsed.input) ? parsed.input : [parsed.input];
          const data = inputs.map((text, index) => ({ index, embedding: fakeEmbed(text) }));
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ data }));
          return;
        }
        if (req.url?.endsWith("/rerank")) {
          const parsed = JSON.parse(body) as {
            candidates: Array<{ id: string; text: string; score: number }>;
          };
          const ranked = parsed.candidates.map((c) => ({
            id: c.id,
            score: c.score,
            original_score: c.score,
          }));
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify(ranked));
          return;
        }
        res.writeHead(404).end();
      } catch (err) {
        res.writeHead(500).end(String(err));
      }
    });
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;

  return {
    url: `http://127.0.0.1:${port}`,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}
