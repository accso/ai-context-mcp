import { test, describe, before, after } from "node:test";
import assert from "node:assert/strict";
import { analyzeImpact } from "../../src/db";
import { startTestPostgres, stopTestPostgres, TestDb } from "./test-postgres";

// Tier 2: real disposable Postgres (postgres:16-alpine, the real schema
// from infrastructure/init.sql) — not a mock, so these tests exercise the
// actual SQL/constraints analyze_impact's traversal depends on.
//
// Fixture call graph seeded below:
//   main() --calls--> route() --calls--> handle_order_created()
//   main() --calls--> ambiguous_func()  [resolution='ambiguous', 2 candidates]
//   test_main.py imports main.py        [import_edges, for affected_test_files]

const PROJECT_KEY = "test_project";

let db: TestDb;

before(async () => {
  db = await startTestPostgres();
  const { pool } = db;

  await pool.query(
    `INSERT INTO projects (project_key, display_name, profile, root_path) VALUES ($1, $2, $3, $4)`,
    [PROJECT_KEY, "Test Project", "{}", ""]
  );

  // symbol_index: id 1=main, 2=route, 3=handle_order_created,
  // 4/5 = two candidates both named "ambiguous_func" (for ambiguity tests).
  const symbols: Array<[string, string, string, number, number]> = [
    ["main.py", "main", "function", 1, 5],
    ["dispatcher.py", "route", "function", 1, 10],
    ["dispatcher.py", "handle_order_created", "function", 11, 13],
    ["a.py", "ambiguous_func", "function", 1, 3],
    ["b.py", "ambiguous_func", "function", 1, 3],
  ];
  for (const [file_path, symbol_name, symbol_kind, start_line, end_line] of symbols) {
    await pool.query(
      `INSERT INTO symbol_index (project_key, file_path, symbol_name, symbol_kind, start_line, end_line)
       VALUES ($1, $2, $3, $4, $5, $6)`,
      [PROJECT_KEY, file_path, symbol_name, symbol_kind, start_line, end_line]
    );
  }
  const idFor = async (file_path: string, symbol_name: string): Promise<number> => {
    const res = await pool.query(
      `SELECT id FROM symbol_index WHERE project_key=$1 AND file_path=$2 AND symbol_name=$3`,
      [PROJECT_KEY, file_path, symbol_name]
    );
    return res.rows[0].id;
  };
  const mainId = await idFor("main.py", "main");
  const routeId = await idFor("dispatcher.py", "route");
  const handlerId = await idFor("dispatcher.py", "handle_order_created");

  await pool.query(
    `INSERT INTO call_edges (project_key, caller_file, caller_symbol_id, callee_name, callee_symbol_id, resolution, call_line)
     VALUES
       ($1, 'main.py', $2, 'route', $3, 'exact', 3),
       ($1, 'dispatcher.py', $3, 'handle_order_created', $4, 'exact', 8),
       ($1, 'main.py', $2, 'ambiguous_func', NULL, 'ambiguous', 4)`,
    [PROJECT_KEY, mainId, routeId, handlerId]
  );

  await pool.query(
    `INSERT INTO import_edges (project_key, importer_file, specifier, resolved_file, import_kind, line)
     VALUES ($1, 'src/tests/test_main.py', 'main', 'main.py', 'py_import', 1)`,
    [PROJECT_KEY]
  );
});

after(async () => {
  await stopTestPostgres(db);
});

describe("analyzeImpact", () => {
  test("direction=callees traverses the real call graph across two hops", async () => {
    const result = await analyzeImpact(db.pool, PROJECT_KEY, "main", undefined, "callees", 2, false);
    assert.equal(result.error, undefined);
    assert.equal(result.symbol?.file, "main.py");
    const bySymbol = Object.fromEntries(result.callees.map((c) => [c.symbol, c]));
    assert.equal(bySymbol["route"].hops, 1);
    assert.equal(bySymbol["handle_order_created"].hops, 2);
  });

  test("direction=callers traverses backward across two hops", async () => {
    const result = await analyzeImpact(
      db.pool, PROJECT_KEY, "handle_order_created", undefined, "callers", 2, false
    );
    const bySymbol = Object.fromEntries(result.callers.map((c) => [c.symbol, c]));
    assert.equal(bySymbol["route"].hops, 1);
    assert.equal(bySymbol["main"].hops, 2);
  });

  test("depth=1 stops after the first hop", async () => {
    const result = await analyzeImpact(db.pool, PROJECT_KEY, "main", undefined, "callees", 1, false);
    const names = result.callees.map((c) => c.symbol);
    assert.deepEqual(names, ["route"]);
  });

  test("ambiguous call edges are surfaced separately with candidates, not silently guessed", async () => {
    const result = await analyzeImpact(db.pool, PROJECT_KEY, "main", undefined, "callees", 1, false);
    assert.equal(result.ambiguous_edges.length, 1);
    assert.equal(result.ambiguous_edges[0].callee_name, "ambiguous_func");
    assert.equal(result.ambiguous_edges[0].candidates.length, 2);
  });

  test("unknown symbol name returns not_found, not a crash", async () => {
    const result = await analyzeImpact(db.pool, PROJECT_KEY, "NoSuchSymbol", undefined, "both", 1, false);
    assert.equal(result.error, "not_found");
    assert.equal(result.symbol, null);
  });

  test("ambiguous target (same name, multiple files, no file_path) reports ambiguous_target", async () => {
    const result = await analyzeImpact(db.pool, PROJECT_KEY, "ambiguous_func", undefined, "both", 1, false);
    assert.equal(result.error, "ambiguous_target");
    assert.match(result.blast_radius_summary, /a\.py.*b\.py|b\.py.*a\.py/);
  });

  test("file_path disambiguates an otherwise-ambiguous symbol name", async () => {
    const result = await analyzeImpact(db.pool, PROJECT_KEY, "ambiguous_func", "a.py", "both", 1, false);
    assert.equal(result.error, undefined);
    assert.equal(result.symbol?.file, "a.py");
  });

  test("affected_test_files resolves via import_edges, not just call_edges", async () => {
    // src/tests/test_main.py never CALLS main() in this fixture — it only
    // imports it. collectAffectedTestFiles must still surface it via the
    // import_edges path (matches TEST_FILE_PATTERN's `/tests/` clause since
    // it's nested under src/ — see the two known-gap tests below for paths
    // that do NOT match this heuristic today).
    const result = await analyzeImpact(db.pool, PROJECT_KEY, "main", undefined, "callees", 1, true);
    assert.ok(result.affected_test_files.includes("src/tests/test_main.py"));
  });

  test("KNOWN GAP: Python's own test_*.py filename convention is not recognized", async () => {
    // TEST_FILE_PATTERN is /(\.(test|spec)\.[^/]+$)|(\/__tests__\/)|(\/tests\/)/i
    // — JS/TS-centric (`.test.js`, `.spec.ts`, `__tests__/`, nested `/tests/`).
    // A bare `test_main.py` (pytest's own convention, used throughout THIS
    // test suite) matches none of those clauses, so a Python project's test
    // coverage is silently under-reported by analyze_impact today. Not
    // fixed as part of this test-writing task — documented so it can't
    // regress further unnoticed, and so it's easy to point at when scoping
    // a fix (add a `test_[^/]+\.py$` clause, or a language-aware pattern set).
    const { pool } = db;
    await pool.query(
      `INSERT INTO import_edges (project_key, importer_file, specifier, resolved_file, import_kind, line)
       VALUES ($1, 'test_route.py', 'route', 'dispatcher.py', 'py_import', 1)`,
      [PROJECT_KEY]
    );
    const result = await analyzeImpact(db.pool, PROJECT_KEY, "route", undefined, "callees", 1, true);
    assert.equal(result.affected_test_files.includes("test_route.py"), false); // should be true once fixed
  });

  test("KNOWN GAP: a top-level tests/ directory (no preceding path segment) is not recognized", async () => {
    // TEST_FILE_PATTERN's `/tests/` clause requires a literal '/' BEFORE
    // "tests" too — so "tests/foo.py" (a top-level tests dir, no nesting,
    // e.g. this very repo's own .ai-context/tests/fixtures/ layout) does
    // NOT match, only a NESTED .../tests/... does (as used in the positive
    // test above). Same "not fixed here, documented so it can't regress
    // further" rationale as the gap above.
    const { pool } = db;
    await pool.query(
      `INSERT INTO import_edges (project_key, importer_file, specifier, resolved_file, import_kind, line)
       VALUES ($1, 'tests/test_handler.py', 'handle_order_created', 'dispatcher.py', 'py_import', 1)`,
      [PROJECT_KEY]
    );
    const result = await analyzeImpact(db.pool, PROJECT_KEY, "handle_order_created", undefined, "callees", 1, true);
    assert.equal(result.affected_test_files.includes("tests/test_handler.py"), false); // should be true once fixed
  });

  test("blast_radius_summary reports caller count and test coverage gap", async () => {
    const result = await analyzeImpact(
      db.pool, PROJECT_KEY, "handle_order_created", undefined, "callers", 1, true
    );
    assert.match(result.blast_radius_summary, /1 caller\(s\) across 1 file\(s\)/);
    assert.match(result.blast_radius_summary, /no matching test file/);
  });
});
