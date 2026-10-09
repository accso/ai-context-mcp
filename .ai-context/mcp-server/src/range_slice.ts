/**
 * range_slice.ts
 *
 * Shared helpers for slicing indexed content (chunk or doc-tree node text)
 * to an exact requested line range without reading from disk.
 */

export type ChunkPayload = {
  text: string;
  start_line: number;
  end_line: number;
  symbol?: string | null;
  language?: string;
};

export type SlicedPiece = { start: number; end: number; lines: string[] };

export function sliceChunk(
  c: ChunkPayload,
  requestedStart: number,
  requestedEnd: number
): SlicedPiece | null {
  const chunkLines = c.text.split("\n");
  const expectedLen = c.end_line - c.start_line + 1;
  if (chunkLines.length !== expectedLen) {
    // Stored text doesn't match the stored line span (stale/drifted payload).
    // Do not attempt to slice — return the whole chunk instead, so we never
    // emit content mislabeled with wrong line numbers.
    return { start: c.start_line, end: c.end_line, lines: chunkLines };
  }
  const localStart = Math.max(requestedStart, c.start_line) - c.start_line;
  const localEnd = Math.min(requestedEnd, c.end_line) - c.start_line;
  if (localStart > localEnd) return null; // no actual overlap; caller should filter these out beforehand
  return {
    start: c.start_line + localStart,
    end: c.start_line + localEnd,
    lines: chunkLines.slice(localStart, localEnd + 1),
  };
}

export function describeCoverageGaps(
  pieces: SlicedPiece[],
  requestedStart: number,
  requestedEnd: number
): string {
  const gaps: string[] = [];
  let cursor = requestedStart;
  for (const p of pieces) {
    if (p.start > cursor) gaps.push(`${cursor}-${p.start - 1}`);
    cursor = Math.max(cursor, p.end + 1);
  }
  if (cursor <= requestedEnd) gaps.push(`${cursor}-${requestedEnd}`);
  return gaps.length
    ? `\n\n_(no indexed content for line(s) ${gaps.join(", ")} within the requested range)_`
    : "";
}
