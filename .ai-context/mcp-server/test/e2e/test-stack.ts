/**
 * test-stack.ts — orchestrates a full, real E2E stack for Tier 3 tests:
 * disposable Postgres + Qdrant (test-postgres.ts / test-qdrant.ts), the
 * REAL watcher indexing pipeline (run as a subprocess — see
 * tests/e2e/index_fixture_repo.py — against the shared fixture corpus),
 * a fake inference HTTP server (fake-inference-server.ts, standing in for
 * .ai-context/inference/ — embeddings + re-ranking), and the REAL
 * mcp-server project_server.ts Express app (not a mock, not a re-implementation)
 * listening on a real port, reachable via a real MCP client over
 * StreamableHTTP.
 *
 * This is deliberately NOT built from the production Dockerfiles/
 * docker-compose.yml — building+orchestrating full container images for
 * watcher/mcp-server would multiply this suite's cost for no additional
 * coverage of anything this project's own code controls (the inference/LLM
 * services are always external anyway, real or not). What matters for E2E
 * confidence — the real indexer code, the real schema, the real MCP
 * server/tool code, a real HTTP+MCP-protocol round trip — is exactly what
 * this DOES exercise.
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { Pool } from "pg";
import { createProjectServer } from "../../src/project_server";
import type { ProjectConfig } from "../../src/config";
import { startTestPostgres, stopTestPostgres, TestDb } from "../integration/test-postgres";
import { startTestQdrant, stopTestQdrant, TestQdrant } from "./test-qdrant";
import { startFakeInferenceServer, FakeInferenceServer } from "./fake-inference-server";

const execFileAsync = promisify(execFile);

export const PROJECT_KEY = "e2e_fixture_project";
export const MCP_TEST_PORT = 17100;

const FIXTURE_ROOT = path.resolve(__dirname, "../../../tests/fixtures/sample-repo");
const INDEX_SCRIPT = path.resolve(__dirname, "../../../tests/e2e/index_fixture_repo.py");

export interface E2EStack {
  client: Client;
  pool: Pool;
  close: () => Promise<void>;
}

export async function startE2EStack(): Promise<E2EStack> {
  const pg = await startTestPostgres();
  const qdrant = await startTestQdrant();
  const inferenceServer = await startFakeInferenceServer();

  // Run the REAL watcher indexing pipeline against the fixture corpus, in
  // its own Python process — see index_fixture_repo.py's module docstring
  // for why (this codebase's watcher is Python; this test file is TS).
  await execFileAsync("python3", [INDEX_SCRIPT], {
    env: {
      ...process.env,
      PROJECT_KEY,
      PROJECT_ROOT: FIXTURE_ROOT,
      POSTGRES_URL: pg.connectionString,
      QDRANT_URL: qdrant.url,
    },
  });

  // FINDING (discovered via this E2E suite): context_assembler.ts's
  // deriveQdrantUrl() reads process.env.QDRANT_URL directly instead of
  // using the qdrantUrl value passed through SharedServices/createProjectServer
  // — unlike get_file (project_server.ts:506), which correctly uses the
  // injected services.qdrantUrl. In production this is harmless because
  // both are always set from the same source at container startup, but
  // it's a real DI inconsistency: semantic_search silently ignores whatever
  // qdrantUrl this process is constructed with. Setting the env var here
  // (not fixing context_assembler.ts, which is out of scope for a
  // test-writing change) is the correct workaround, matching production's
  // own implicit assumption.
  process.env.QDRANT_URL = qdrant.url;

  const project: ProjectConfig = {
    name: PROJECT_KEY,
    display_name: "E2E Fixture Project",
    profile: { components: [], monorepo: false },
    root_path: "",
    mcp: { port: MCP_TEST_PORT, max_context_tokens: 8000 },
    llm: { provider: "lmstudio", lmstudio_base_url: inferenceServer.url, model: "fake-llm" },
    indexer: {
      debounce_seconds: 2, extra_skip_paths: [],
      include_extensions: [
        ".kt", ".ts", ".tsx", ".js", ".jsx", ".vue", ".py",
        ".java", ".cs", ".md", ".sql", ".yaml", ".yml",
      ],
    },
    sources: [],
  };

  const pool = pg.pool;
  const app = createProjectServer(project, {
    pool,
    qdrantUrl: qdrant.url,
    inferenceUrl: inferenceServer.url,
    rerankerTopN: 20,
    lspBridgeUrl: null,
  });

  const httpServer = await new Promise<import("http").Server>((resolve) => {
    const s = app.listen(MCP_TEST_PORT, () => resolve(s));
  });

  const transport = new StreamableHTTPClientTransport(new URL(`http://localhost:${MCP_TEST_PORT}/mcp`));
  const client = new Client({ name: "e2e-test-client", version: "1.0.0" });
  await client.connect(transport);

  return {
    client,
    pool,
    close: async () => {
      await client.close().catch(() => {});
      await new Promise<void>((resolve) => httpServer.close(() => resolve()));
      await inferenceServer.close();
      await stopTestQdrant(qdrant);
      await stopTestPostgres(pg);
    },
  };
}
