/**
 * register-js-to-ts.js — Node CJS resolution patch for running this
 * project's TypeScript sources directly via ts-node (no compile step).
 *
 * Every src/*.ts file imports its siblings with a `.js` suffix (e.g.
 * `./embedder.js`), which `tsc` resolves correctly for the compiled dist/
 * output. ts-node's require hook only intercepts `.ts`/`.tsx` extensions —
 * it does not know that a literal `.js` specifier should fall back to a
 * sibling `.ts` file when no compiled `.js` exists. This patches Node's
 * CJS module resolution to retry `.js` -> `.ts` before giving up, so
 * `context_assembler.ts` (and anything it imports) can be required
 * directly by tests without a build step.
 *
 * Load BEFORE ts-node/register (see package.json test scripts): this only
 * changes *which path* gets resolved; ts-node's own hook still does the
 * actual transpilation once resolution lands on a `.ts` file.
 */
const Module = require("module");

const originalResolveFilename = Module._resolveFilename;

Module._resolveFilename = function (request, ...rest) {
  if (request.endsWith(".js")) {
    const tsRequest = request.slice(0, -3) + ".ts";
    try {
      return originalResolveFilename.call(this, tsRequest, ...rest);
    } catch {
      // No sibling .ts file either — fall through to normal resolution
      // (compiled dist/ output, a real .js file, or a node_modules package).
    }
  }
  return originalResolveFilename.call(this, request, ...rest);
};
