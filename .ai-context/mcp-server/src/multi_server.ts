/**
 * multi_server.ts — AI Context MCP Server (multi-project entrypoint)
 *
 * Loads the repository-wide .resolved.json once, creates one Express app per
 * configured project via project_server.ts, and starts each on its own port.
 *
 * Design constraints honoured here:
 *   - Project identity is determined only by the port/endpoint a request
 *     arrives on. No `project` parameter is added to any tool or route.
 *   - One broken project does not prevent the others from starting.
 *   - Each project gets its own PostgreSQL Pool (connection reuse within the
 *     project, isolation across projects).
 */

import fs from "fs";
import { Pool } from "pg";
import { type ProjectConfig, type Resolved } from "./config.js";
import { createProjectServer, type SharedServices } from "./project_server.js";

// ── Config loading (once) ─────────────────────────────────────────────────────

function loadResolved(): Resolved {
  const resolvedPath = process.env.RESOLVED_PATH ?? "/app/.resolved.json";
  return JSON.parse(fs.readFileSync(resolvedPath, "utf8")) as Resolved;
}

function getEnvOrDefault(name: string, fallback: string): string {
  return process.env[name] ?? fallback;
}

// ── Error context tracking ────────────────────────────────────────────────────
// Used by global handlers to identify which project's startup/request context
// was active when an unhandled error occurred.

let activeProjectName = "<startup>";

process.on("unhandledRejection", (reason) => {
  console.error(`[${activeProjectName}] Unhandled promise rejection`, { reason: String(reason) });
});

process.on("uncaughtException", (err) => {
  console.error(`[${activeProjectName}] Uncaught exception — process will exit`, {
    error: String(err),
    stack: err.stack ?? "",
  });
  process.exit(1);
});

// ── Per-project startup ───────────────────────────────────────────────────────

interface StartedServer {
  projectName: string;
  port: number;
  pool: Pool;
}

async function startProject(
  project: ProjectConfig,
  qdrantUrl: string,
  postgresUrl: string,
  inferenceUrl: string | null,
  rerankerTopN: number,
  lspBridgeUrl: string | null
): Promise<StartedServer | null> {
  const previousContext = activeProjectName;
  activeProjectName = project.name;

  try {
    const pool = new Pool({ connectionString: postgresUrl });

    pool.on("error", (err) => {
      console.error(`[${project.name}] PostgreSQL pool error`, { error: String(err) });
    });

    const services: SharedServices = {
      pool,
      qdrantUrl,
      inferenceUrl,
      rerankerTopN,
      lspBridgeUrl,
    };

    const app = createProjectServer(project, services);
    const port = project.mcp.port;

    await new Promise<void>((resolve, reject) => {
      const server = app.listen(port, () => {
        console.log(`[${project.name}] AI Context MCP Server listening`, {
          port,
          project: project.name,
          display: project.display_name,
          mcp:     `http://localhost:${port}/mcp`,
          health:  `http://localhost:${port}/health`,
        });
        resolve();
      });

      server.on("error", (err) => {
        reject(err);
      });
    });

    return { projectName: project.name, port, pool };
  } catch (err) {
    console.error(`[${project.name}] Failed to start MCP server`, { error: String(err) });
    return null;
  } finally {
    activeProjectName = previousContext;
  }
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  const resolved = loadResolved();
  const projects = Object.values(resolved.projects);

  const qdrantUrl = getEnvOrDefault("QDRANT_URL", "http://localhost:6333");
  const postgresUrl = getEnvOrDefault(
    "POSTGRES_URL",
    "postgresql://ai_context:ai_context_local@localhost:5432/ai_context"
  );
  const inferenceUrl = process.env.INFERENCE_URL ?? null;
  const rerankerTopN = parseInt(process.env.RERANKER_TOP_N ?? "20", 10);
  const lspBridgeUrl = process.env.LSP_BRIDGE_URL ?? null;

  console.log(`Starting AI Context MCP servers for ${projects.length} project(s) ...`);

  const started: StartedServer[] = [];
  for (const project of projects) {
    const server = await startProject(project, qdrantUrl, postgresUrl, inferenceUrl, rerankerTopN, lspBridgeUrl);
    if (server) {
      started.push(server);
    }
  }

  if (started.length === 0) {
    console.error("No MCP servers started successfully. Exiting.");
    process.exit(1);
  }

  console.log(`All available MCP servers started successfully (${started.length}/${projects.length}).`);

  // Graceful shutdown: close all pools on SIGTERM/SIGINT.
  async function shutdown(signal: string) {
    console.log(`Received ${signal} — shutting down ${started.length} MCP server(s) ...`);
    for (const s of started) {
      try {
        await s.pool.end();
        console.log(`[${s.projectName}] PostgreSQL pool closed.`);
      } catch (err) {
        console.error(`[${s.projectName}] Error closing pool`, { error: String(err) });
      }
    }
    process.exit(0);
  }

  process.on("SIGTERM", () => void shutdown("SIGTERM"));
  process.on("SIGINT", () => void shutdown("SIGINT"));
}

void main();
