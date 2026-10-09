import { test, describe, before, beforeEach, after } from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import os from "node:os";
import fs from "node:fs";
import axios from "axios";
import { startTestPostgres, stopTestPostgres, TestDb } from "../integration/test-postgres";
import { startTestQdrant, stopTestQdrant, TestQdrant } from "./test-qdrant";

const execFileAsync = promisify(execFile);

const PROJECT_KEY = "e2e_backup_restore_project";
const FIXTURE_ROOT = path.resolve(__dirname, "../../../tests/fixtures/sample-repo");
const INDEX_SCRIPT = path.resolve(__dirname, "../../../tests/e2e/index_fixture_repo.py");
const BACKUP_STEP_SCRIPT = path.resolve(__dirname, "../../../tests/e2e/backup_restore_step.py");

let pg: TestDb;
let qdrant: TestQdrant;
let backupDir: string;

async function runStep(step: "backup" | "wipe" | "restore"): Promise<void> {
  await execFileAsync("python3", [BACKUP_STEP_SCRIPT], {
    env: {
      ...process.env,
      STEP: step,
      PROJECT_KEY,
      PROJECT_ROOT: FIXTURE_ROOT,
      POSTGRES_URL: pg.connectionString,
      QDRANT_URL: qdrant.url,
      BACKUP_DIR: backupDir,
      BACKUP_FORMAT: "json", // avoids the Qdrant snapshot upload/download path — see backup.py's own format comparison
    },
  });
}

async function tableCounts(): Promise<Record<string, number>> {
  const tables = ["projects", "file_index", "l1_summaries", "l0_maps", "doc_trees"];
  const counts: Record<string, number> = {};
  for (const t of tables) {
    const res = await pg.pool.query(`SELECT COUNT(*) FROM ${t} WHERE project_key = $1`, [PROJECT_KEY]);
    counts[t] = parseInt(res.rows[0].count, 10);
  }
  return counts;
}

async function qdrantPointCount(): Promise<number> {
  const resp = await axios.get(`${qdrant.url}/collections/${PROJECT_KEY}`);
  return resp.data.result.points_count;
}

async function reindexFixture(): Promise<void> {
  await execFileAsync("python3", [INDEX_SCRIPT], {
    env: {
      ...process.env,
      PROJECT_KEY,
      PROJECT_ROOT: FIXTURE_ROOT,
      POSTGRES_URL: pg.connectionString,
      QDRANT_URL: qdrant.url,
    },
  });
}

before(async () => {
  pg = await startTestPostgres();
  qdrant = await startTestQdrant();
  backupDir = fs.mkdtempSync(path.join(os.tmpdir(), "ai-context-e2e-backup-"));
});

// Each test gets a fresh index — tests 1/2 wipe real state as part of the
// backup->wipe->restore cycle they're testing, so sharing one indexed
// state across tests (session-level before()) would make later tests see
// already-wiped/partially-restored data, not the pristine post-index
// state they need. Slower (~1 real index per test) but genuinely
// independent, which matters more for an E2E suite whose whole point is
// verifying a destructive operation (wipe) is correctly reversed.
beforeEach(reindexFixture);

after(async () => {
  fs.rmSync(backupDir, { recursive: true, force: true });
  await stopTestQdrant(qdrant);
  await stopTestPostgres(pg);
});

describe("Backup/restore E2E: real indexed state survives a wipe", () => {
  test("backup -> wipe -> restore reproduces the pre-backup PostgreSQL row counts", async () => {
    const before_ = await tableCounts();
    assert.ok(before_.file_index > 0, "sanity check: fixture repo actually got indexed");

    await runStep("backup");

    const backupFiles = fs.readdirSync(backupDir);
    assert.ok(backupFiles.includes("postgres.json"));
    assert.ok(backupFiles.includes("qdrant.vectors.json"));
    assert.ok(backupFiles.includes("backup_meta.json"));

    await runStep("wipe");
    const afterWipe = await tableCounts();
    assert.equal(afterWipe.projects, 0, "wipe must actually remove the project row");
    assert.equal(afterWipe.file_index, 0);

    await runStep("restore");
    const afterRestore = await tableCounts();
    assert.deepEqual(afterRestore, before_);
  });

  test("backup -> wipe -> restore reproduces the pre-backup Qdrant point count", async () => {
    const beforeCount = await qdrantPointCount();
    assert.ok(beforeCount > 0, "sanity check: fixture repo actually produced Qdrant points");

    await runStep("backup");
    await runStep("wipe");
    const afterWipeCount = await qdrantPointCount();
    assert.equal(afterWipeCount, 0);

    await runStep("restore");
    const afterRestoreCount = await qdrantPointCount();
    assert.equal(afterRestoreCount, beforeCount);
  });

  test("KNOWN GAP: symbol_index/import_edges/call_edges are NOT covered by backup/restore", async () => {
    // Documents the gap already flagged in docs/overview.html /
    // context-map.html corrections: backup.py's _TABLES only covers
    // projects/file_index/l1_summaries/l0_maps/doc_trees. Confirmed here
    // against a REAL indexed project (this fixture repo has real
    // symbol_index/call_edges rows from the real indexer, not synthetic
    // ones) — restore does not recreate them; only a reindex does.
    const before_ = await pg.pool.query(
      "SELECT COUNT(*) FROM symbol_index WHERE project_key = $1", [PROJECT_KEY]
    );
    assert.ok(parseInt(before_.rows[0].count, 10) > 0, "sanity check: real symbols were extracted");

    await runStep("backup");
    await runStep("wipe");
    await runStep("restore");

    const afterRestore = await pg.pool.query(
      "SELECT COUNT(*) FROM symbol_index WHERE project_key = $1", [PROJECT_KEY]
    );
    assert.equal(parseInt(afterRestore.rows[0].count, 10), 0); // NOT restored — known gap, not a regression
  });
});
