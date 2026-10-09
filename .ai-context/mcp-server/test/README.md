# mcp-server/test/

Tier-based test directories (`unit/`, `integration/`, `e2e/`) using Node's
built-in `node --test` + the already-present `ts-node` devDependency — no
new test framework dependency. Run via `npm run test:unit` /
`test:integration` / `test:e2e` / `test`, or via `scripts/test.sh` at the
repo root for the cross-component, tier-selectable entrypoint.

**Import convention — importing from `src/` in test files works both with
and without the `.js` extension.** Source files import each other with a
`.js` suffix (e.g. `./context_assembler.js`), which `tsc` resolves
correctly for the compiled `dist/` output. Test files run directly via
`ts-node`'s CommonJS require hook (no compile step) — plain Node CJS
resolution doesn't know a `.js` specifier should fall back to a sibling
`.ts` file, and this bites transitively too (e.g. `context_assembler.ts`
itself imports `./embedder.js`, `./qdrant.js`, etc.). `test/register-js-to-ts.js`
patches `Module._resolveFilename` to retry `.js` -> `.ts` before failing,
and is loaded via `--require` **before** `ts-node/register` in every
`test:*` npm script — so any `src/` module can be required directly by
tests, regardless of which extension convention it uses internally.

This directory is outside `tsconfig.json`'s `include` (`src/**/*`), so
`npm run build` never compiles or ships anything from here.
