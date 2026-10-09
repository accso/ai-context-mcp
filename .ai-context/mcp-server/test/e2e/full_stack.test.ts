import { test, describe, before, after } from "node:test";
import assert from "node:assert/strict";
import { startE2EStack, E2EStack } from "./test-stack";

// Tier 3 E2E: full real pipeline — real disposable Postgres + Qdrant, the
// REAL watcher indexing code (indexer.run_full_index + l0_generator.run_l0,
// run as a subprocess against the shared fixture corpus), the REAL
// project_server.ts Express app, reached via a REAL MCP client over
// StreamableHTTP. Only the embedding/LLM HTTP calls are stubbed (see
// fake-embedding-server.ts / index_fixture_repo.py) — those are always
// external services in production too, real or not.
//
// resolve_symbol (needs the LSP bridge, real language servers) is
// deliberately out of scope here — see test-e2e-lsp-resolve-symbol, which
// depends on this suite and adds that coverage separately (cost-bounded,
// per the test plan).

let stack: E2EStack;

before(async () => {
  stack = await startE2EStack();
});

after(async () => {
  await stack.close();
});

describe("Full-stack E2E: real indexing -> real MCP tools", () => {
  test("repo_structure shows the real indexed fixture files", async () => {
    const result = await stack.client.callTool({ name: "repo_structure", arguments: { depth: 4 } });
    const text = (result.content as Array<{ text: string }>)[0].text;
    assert.match(text, /dispatcher\.py/);
    assert.match(text, /UserService\.kt/);
    assert.match(text, /OrderService\.java/);
  });

  test("semantic_search returns real hits from the indexed corpus", async () => {
    const result = await stack.client.callTool({
      name: "semantic_search",
      arguments: { query: "route dispatcher" },
    });
    const text = (result.content as Array<{ text: string }>)[0].text;
    // Structural check only — the fake embedding scheme isn't semantically
    // meaningful (see fake-embedding-server.ts), so we assert the pipeline
    // produced real ranked hits from real indexed files, not that ranking
    // quality is good (that's the real embedding model's job).
    assert.match(text, /\.py|\.kt|\.ts|\.java|\.cs/);
    assert.doesNotMatch(text, /No results for/);
  });

  test("get_file returns the exact real file content, not a placeholder", async () => {
    const result = await stack.client.callTool({
      name: "get_file",
      arguments: { file_path: "src/python/dispatcher.py" },
    });
    const text = (result.content as Array<{ text: string }>)[0].text;
    assert.match(text, /def route\(event_type: str, payload: dict\)/);
    assert.match(text, /def handle_order_created/);
  });

  test("analyze_impact traces the real call graph the real indexer built", async () => {
    // main.py's main() really does call dispatcher.route() in the fixture
    // corpus — this exercises the REAL symbol_extractor.py + indexer.py
    // call-edge extraction end-to-end, not synthetic seeded rows (unlike
    // the Tier 2 analyze_impact suite, which seeds call_edges directly).
    // file_path disambiguates: "route" also names a method in
    // notification.service.ts's NotificationService fixture.
    const result = await stack.client.callTool({
      name: "analyze_impact",
      arguments: {
        symbol_name: "route", file_path: "src/python/dispatcher.py",
        direction: "callers", depth: 1,
      },
    });
    const text = (result.content as Array<{ text: string }>)[0].text;
    assert.match(text, /main/);
    assert.doesNotMatch(text, /No symbol named/);
  });

  test("resolve_symbol degrades gracefully when no LSP bridge is configured", async () => {
    const result = await stack.client.callTool({
      name: "resolve_symbol",
      arguments: { file_path: "src/python/dispatcher.py", line: 1, character: 0, kind: "definition" },
    });
    const text = (result.content as Array<{ text: string }>)[0].text;
    // Must explain why, not silently error or crash the server.
    assert.equal(typeof text, "string");
    assert.ok(text.length > 0);
  });
});
