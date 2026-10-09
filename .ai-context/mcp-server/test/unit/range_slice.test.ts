import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { sliceChunk, describeCoverageGaps, ChunkPayload, SlicedPiece } from "../../src/range_slice";

// Regression suite for get_file's exact-line-range slicing — this module
// directly fixed a historical P0 bug (get_file returning wrong line ranges
// instead of exact requested slices). These tests exist so that bug class
// can never silently reappear.

function chunk(startLine: number, endLine: number, text: string): ChunkPayload {
  return { text, start_line: startLine, end_line: endLine };
}

describe("sliceChunk", () => {
  test("exact match: requested range equals chunk range returns the whole chunk", () => {
    const c = chunk(10, 12, "line10\nline11\nline12");
    const result = sliceChunk(c, 10, 12);
    assert.deepEqual(result, { start: 10, end: 12, lines: ["line10", "line11", "line12"] });
  });

  test("requested range fully inside chunk returns only the requested sub-slice", () => {
    const c = chunk(10, 20, Array.from({ length: 11 }, (_, i) => `line${10 + i}`).join("\n"));
    const result = sliceChunk(c, 13, 15);
    assert.deepEqual(result, { start: 13, end: 15, lines: ["line13", "line14", "line15"] });
  });

  test("requested range starts before chunk (left overlap) clips to chunk start", () => {
    const c = chunk(10, 15, "l10\nl11\nl12\nl13\nl14\nl15");
    const result = sliceChunk(c, 5, 12);
    assert.deepEqual(result, { start: 10, end: 12, lines: ["l10", "l11", "l12"] });
  });

  test("requested range ends after chunk (right overlap) clips to chunk end", () => {
    const c = chunk(10, 15, "l10\nl11\nl12\nl13\nl14\nl15");
    const result = sliceChunk(c, 13, 100);
    assert.deepEqual(result, { start: 13, end: 15, lines: ["l13", "l14", "l15"] });
  });

  test("requested range is a superset of the chunk returns the whole chunk", () => {
    const c = chunk(10, 12, "l10\nl11\nl12");
    const result = sliceChunk(c, 1, 1000);
    assert.deepEqual(result, { start: 10, end: 12, lines: ["l10", "l11", "l12"] });
  });

  test("single-line overlap at the exact boundary still returns that one line", () => {
    // requested=[1,5], chunk=[5,10] — only line 5 overlaps.
    const c = chunk(5, 10, "l5\nl6\nl7\nl8\nl9\nl10");
    const result = sliceChunk(c, 1, 5);
    assert.deepEqual(result, { start: 5, end: 5, lines: ["l5"] });
  });

  test("no overlap: requested range entirely before the chunk returns null", () => {
    const c = chunk(10, 15, "l10\nl11\nl12\nl13\nl14\nl15");
    assert.equal(sliceChunk(c, 1, 5), null);
  });

  test("no overlap: requested range entirely after the chunk returns null", () => {
    const c = chunk(10, 15, "l10\nl11\nl12\nl13\nl14\nl15");
    assert.equal(sliceChunk(c, 20, 25), null);
  });

  test("single-line chunk exact match", () => {
    const c = chunk(42, 42, "only line");
    const result = sliceChunk(c, 42, 42);
    assert.deepEqual(result, { start: 42, end: 42, lines: ["only line"] });
  });

  test("stale/drifted payload (text length doesn't match declared span) returns the whole chunk instead of mis-slicing", () => {
    // Declared span is 5 lines (10-14) but the stored text only has 3 —
    // exactly the "stale/drifted payload" case the function guards against.
    // Slicing this with line-offset math would silently attribute the wrong
    // text to the wrong line numbers, which is the original bug class.
    const c = chunk(10, 14, "onlyThreeLines\nline2\nline3");
    const result = sliceChunk(c, 11, 12);
    assert.deepEqual(result, { start: 10, end: 14, lines: ["onlyThreeLines", "line2", "line3"] });
  });
});

describe("describeCoverageGaps", () => {
  test("no gap when a single piece covers the entire requested range", () => {
    const pieces: SlicedPiece[] = [{ start: 10, end: 20, lines: [] }];
    assert.equal(describeCoverageGaps(pieces, 10, 20), "");
  });

  test("gap reported before the first piece", () => {
    const pieces: SlicedPiece[] = [{ start: 15, end: 20, lines: [] }];
    const result = describeCoverageGaps(pieces, 10, 20);
    assert.match(result, /10-14/);
  });

  test("gap reported after the last piece", () => {
    const pieces: SlicedPiece[] = [{ start: 10, end: 15, lines: [] }];
    const result = describeCoverageGaps(pieces, 10, 20);
    assert.match(result, /16-20/);
  });

  test("gap reported between two non-adjacent pieces", () => {
    const pieces: SlicedPiece[] = [
      { start: 10, end: 12, lines: [] },
      { start: 16, end: 20, lines: [] },
    ];
    const result = describeCoverageGaps(pieces, 10, 20);
    assert.match(result, /13-15/);
  });

  test("no false gap between adjacent, non-overlapping pieces", () => {
    const pieces: SlicedPiece[] = [
      { start: 10, end: 15, lines: [] },
      { start: 16, end: 20, lines: [] },
    ];
    assert.equal(describeCoverageGaps(pieces, 10, 20), "");
  });

  test("empty pieces array reports the full requested range as one gap", () => {
    const result = describeCoverageGaps([], 10, 20);
    assert.match(result, /10-20/);
  });

  test("overlapping pieces don't produce a negative-range or duplicate gap", () => {
    const pieces: SlicedPiece[] = [
      { start: 10, end: 15, lines: [] },
      { start: 12, end: 20, lines: [] }, // overlaps the first piece
    ];
    assert.equal(describeCoverageGaps(pieces, 10, 20), "");
  });
});
