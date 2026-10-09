# Sample repo — shared test fixture corpus

Deliberately small, multi-language fixture repo used by **all three test
tiers** (see `.ai-context/docs/next-plans/consolidated-backlog-and-serena-roadmap.md`
and the testing plan). Each file below was chosen to exercise a specific,
previously-identified chunking/indexing behavior — not arbitrary sample code.

| File | Exercises |
|---|---|
| `src/kotlin/UserService.kt` | Regex-fallback chunk boundaries (`_KT_TOP`) — Kotlin has no tree-sitter chunk-boundary path yet (see C.9 in the roadmap) |
| `src/typescript/notification.service.ts` | Tree-sitter chunk boundaries (`_chunk_by_treesitter`), Angular decorator-attachment fix (`_extend_start_to_decorator`), and dispatch-arm sub-chunking (`_chunk_dispatch_arms`, a `switch` with 3 arms) |
| `src/vue/UserCard.vue` | Vue SFC block splitting (`_chunk_vue`) — `<script setup>` block (see B.10 in the roadmap: verify whether this gets tree-sitter sub-chunked or stays one opaque block) |
| `src/python/dispatcher.py` | Python `match`/`case` dispatch-arm sub-chunking (3 arms) + regex-fallback top-level boundaries (`_PY_TOP`) |
| `src/python/main.py` | Real import (`from dispatcher import ...`) and call (`route(...)`) relationship with `dispatcher.py`, for `import_extractor.py`/`symbol_extractor.py` call-graph tests |
| `src/java/OrderService.java` | Tree-sitter chunk boundaries for Java |
| `src/csharp/OrderService.cs` | Tree-sitter chunk boundaries for C#, dotted namespace capture |
| `docs/large-guide.md` | Markdown tree-index gate (`should_use_tree_index`) — ≥8 headings, so this becomes a `doc_trees` PageIndex entry with **zero** Qdrant chunks |
| `docs/small-note.md` | Same gate, but below the heading threshold — stays a normal `_chunk_markdown()` Qdrant-chunked file |

Keep this corpus **small and deliberate**. Add a new file only when a new
chunking/indexing behavior needs fixture coverage that none of the existing
files already provide — don't grow it into a realistic-sized project.
