import { test } from "node:test";
import assert from "node:assert/strict";

// Smoke test proving the Tier 1 (unit) test harness works: TypeScript
// compiles via ts-node using tsconfig.test.json, node:test types resolve,
// and `npm run test:unit` picks up files under test/unit/**/*.test.ts.
// Real unit-test suites (context_assembler.ts, range_slice.ts, etc.) are
// added as their own separate change — this file only proves the harness.
test("harness smoke test — unit tier is wired up", () => {
  assert.equal(1 + 1, 2);
});
