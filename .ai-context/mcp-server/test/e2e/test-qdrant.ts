/**
 * test-qdrant.ts — disposable Qdrant container helper for Tier 3 E2E tests.
 * Same conventions as test-postgres.ts: --label com.ai-context.test=true,
 * --rm, a dynamically-assigned host port (`-p 127.0.0.1::6333`, discovered
 * via `docker port`) — not fixed, for the same reason test-postgres.ts
 * isn't: `node --test` can run multiple test files concurrently. Uses
 * plain axios (no @qdrant/js-client-rest dependency — mcp-server's own
 * qdrant.ts doesn't use one either, see src/qdrant.ts) against Qdrant's
 * REST API directly.
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import axios from "axios";

const execFileAsync = promisify(execFile);

const TEST_LABEL = "com.ai-context.test=true";

export interface TestQdrant {
  url: string;
  containerName: string;
}

async function discoverHostPort(containerName: string, containerPort: number): Promise<number> {
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

export async function startTestQdrant(): Promise<TestQdrant> {
  const containerName = `ai_context_test_qdrant_e2e_${process.pid}_${Date.now()}`;
  await execFileAsync("docker", [
    "run", "-d", "--rm",
    "--label", TEST_LABEL,
    "--name", containerName,
    "-p", "127.0.0.1::6333",
    "qdrant/qdrant:v1.18.2",
  ]);

  const hostPort = await discoverHostPort(containerName, 6333);
  const url = `http://localhost:${hostPort}`;

  const deadline = Date.now() + 30_000;
  let lastErr: unknown;
  while (Date.now() < deadline) {
    try {
      await axios.get(`${url}/collections`, { timeout: 2000 });
      return { url, containerName };
    } catch (err) {
      lastErr = err;
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  throw new Error(`Qdrant test container never became ready: ${String(lastErr)}`);
}

export async function stopTestQdrant(db: TestQdrant): Promise<void> {
  await execFileAsync("docker", ["stop", db.containerName]).catch(() => {});
}
