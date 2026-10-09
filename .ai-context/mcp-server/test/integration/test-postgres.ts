/**
 * test-postgres.ts — disposable Postgres container helper for Tier 2
 * integration tests. Reuses the exact image `infrastructure/docker-compose.yml`
 * runs (postgres:16-alpine) and the real schema (infrastructure/init.sql),
 * so integration tests exercise the actual schema/constraints, not an
 * approximation.
 *
 * Every container this starts carries `--label com.ai-context.test=true`
 * and `--rm`, on a dynamically-assigned host port (`-p 127.0.0.1::5432`,
 * discovered via `docker port` after start) — not a fixed port, because
 * `node --test` can run multiple test FILES concurrently (each with its
 * own disposable Postgres), and a fixed port causes exactly the collision
 * a second file's container hits when binding fails. See
 * scripts/test-cleanup.sh, which targets the label, not any specific port.
 * stopContainer() also runs `docker stop`, so on a normal (non-crashed) run
 * cleanup happens immediately, not just via test-cleanup.sh's safety net.
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { Pool } from "pg";

const execFileAsync = promisify(execFile);

const TEST_LABEL = "com.ai-context.test=true";
const INIT_SQL_PATH = path.resolve(__dirname, "../../../infrastructure/init.sql");

export interface TestDb {
  pool: Pool;
  containerName: string;
  connectionString: string;
}

async function discoverHostPort(containerName: string, containerPort: number): Promise<number> {
  // `docker port <name> <containerPort>` prints e.g. "127.0.0.1:54321" once
  // the port mapping is live — retry briefly since it can lag `docker run -d`'s
  // own return by a beat.
  const deadline = Date.now() + 10_000;
  let lastErr: unknown;
  while (Date.now() < deadline) {
    try {
      const { stdout } = await execFileAsync("docker", ["port", containerName, String(containerPort)]);
      const match = stdout.trim().match(/:(\d+)$/);
      if (match) return parseInt(match[1], 10);
    } catch (err) {
      lastErr = err;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`Could not discover host port for ${containerName}:${containerPort}: ${String(lastErr)}`);
}

export async function startTestPostgres(): Promise<TestDb> {
  const containerName = `ai_context_test_pg_${process.pid}_${Date.now()}`;
  await execFileAsync("docker", [
    "run", "-d", "--rm",
    "--label", TEST_LABEL,
    "--name", containerName,
    "-e", "POSTGRES_DB=ai_context_test",
    "-e", "POSTGRES_USER=ai_context",
    "-e", "POSTGRES_PASSWORD=test",
    "-p", "127.0.0.1::5432",
    "-v", `${INIT_SQL_PATH}:/docker-entrypoint-initdb.d/init.sql:ro`,
    "postgres:16-alpine",
  ]);

  const hostPort = await discoverHostPort(containerName, 5432);
  const connectionString = `postgresql://ai_context:test@localhost:${hostPort}/ai_context_test`;
  const pool = new Pool({
    host: "localhost", port: hostPort,
    database: "ai_context_test", user: "ai_context", password: "test",
  });

  // Retry connect — the container needs a moment to run init.sql and
  // accept connections after `docker run -d` returns.
  const deadline = Date.now() + 30_000;
  let lastErr: unknown;
  while (Date.now() < deadline) {
    try {
      await pool.query("SELECT 1");
      return { pool, containerName, connectionString };
    } catch (err) {
      lastErr = err;
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  await pool.end().catch(() => {});
  throw new Error(`Postgres test container never became ready: ${String(lastErr)}`);
}

export async function stopTestPostgres(db: TestDb): Promise<void> {
  await db.pool.end().catch(() => {});
  await execFileAsync("docker", ["stop", db.containerName]).catch(() => {
    // Already stopped/removed (e.g. by scripts/test-cleanup.sh) — fine.
  });
}

