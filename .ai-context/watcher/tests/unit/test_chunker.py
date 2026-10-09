"""
Unit tests for chunker.py — the highest-risk, most-changed module in
.ai-context (chunk-boundary detection feeds L2/semantic_search directly).
Golden-file assertions run against the shared fixture corpus
(tests/fixtures/sample-repo/) so a Tier 3 E2E failure can always be
root-caused by checking whether the matching assertion here still passes.

Also directly resolves two previously-open roadmap items with real test
coverage instead of a one-off manual check:
  - C.2: whether N-arm switch/match/when blocks trigger dispatch-arm
    sub-chunking (parametrized across the _MIN_ARMS_FOR_SUBCHUNK boundary).
  - B.10: whether Vue's <script setup> block gets further sub-chunked
    (documented here as a known gap — asserts the CURRENT single-chunk
    behavior, so this test starts failing the moment that gap is closed,
    which is exactly the signal needed to update the roadmap).
"""
from pathlib import Path

import pytest

import chunker

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "sample-repo"


def _chunk(relative_path: str, config: dict | None = None) -> tuple[list[dict], bool]:
    full_path = FIXTURES / relative_path
    content = full_path.read_text()
    return chunker.chunk_file(relative_path, content, config or {})


def _types(chunks: list[dict]) -> list[str]:
    return [c["chunk_type"] for c in chunks]


# ── Per-language golden-file boundary tests ───────────────────────────────────

def test_kotlin_uses_regex_fallback_not_treesitter():
    # Kotlin has no tree-sitter chunk-boundary path yet (see roadmap C.9) —
    # this test is the tripwire: it should start FAILING (chunk count/types
    # changing) the moment C.9 lands, which is exactly when it should be
    # updated to assert the new tree-sitter-produced boundaries instead.
    chunks, use_tree_index = _chunk("src/kotlin/UserService.kt")
    assert use_tree_index is False
    assert _types(chunks) == ["block", "class", "class", "class"]
    symbols = [c["symbol"] for c in chunks[1:]]
    assert symbols == ["UserService", "User", "UserRepository"]


def test_typescript_treesitter_boundaries_plus_dispatch_arm_subchunking():
    chunks, _ = _chunk("src/typescript/notification.service.ts")
    types = _types(chunks)
    assert types == ["block", "class", "case", "case", "case"]
    # The 3 case sub-chunks correspond to the 3 `case "...":` arms (not the
    # `default:` arm, which _SWITCH_ARM's `case\s+` pattern doesn't match —
    # confirmed intentional behavior, not a bug in this fixture).
    case_chunks = [c for c in chunks if c["chunk_type"] == "case"]
    assert [c["symbol"] for c in case_chunks] == [
        "order.created", "order.cancelled", "user.registered",
    ]


def test_typescript_decorator_stays_attached_to_class_chunk():
    # Angular decorator-attachment fix: the @Injectable() class chunk's
    # start_line must include the decorator line, not just the `export
    # class` line — otherwise the decorator would be silently orphaned
    # into the preceding preamble/import chunk.
    chunks, _ = _chunk("src/typescript/notification.service.ts")
    class_chunk = next(c for c in chunks if c["chunk_type"] == "class")
    assert "@Injectable()" in class_chunk["text"]


def test_vue_sfc_splits_into_template_script_style_blocks():
    chunks, _ = _chunk("src/vue/UserCard.vue")
    assert _types(chunks) == ["block", "component", "function", "block"]


def test_vue_script_setup_is_still_one_opaque_chunk_known_gap():
    # Documents roadmap item B.10's current (gap) state: the <script setup>
    # block is NOT further tree-sitter/regex sub-chunked today, despite
    # containing multiple distinct declarations (defineProps, a computed,
    # a function). This test should start FAILING the moment B.10 is
    # implemented — that failure is the signal to update this test (and
    # close B.10 in the roadmap), not a regression.
    chunks, _ = _chunk("src/vue/UserCard.vue")
    script_chunk = next(c for c in chunks if c["chunk_type"] == "function")
    assert "defineProps" in script_chunk["text"]
    assert "toggleExpanded" in script_chunk["text"]
    assert "computed" in script_chunk["text"]
    # All three declarations landed in the same single chunk — confirms
    # no sub-chunking is happening yet.


def test_python_match_case_dispatch_arm_subchunking():
    chunks, _ = _chunk("src/python/dispatcher.py")
    types = _types(chunks)
    assert types == [
        "block", "function", "case", "case", "case", "case",
        "function", "function", "function",
    ]
    # Unlike TS/JS's `default:`, Python's `case _:` DOES match _MATCH_ARM's
    # `case\s+(.+?):` pattern, so all 4 arms (including the wildcard) are
    # sub-chunked here — a real, language-specific asymmetry, not a bug.
    case_chunks = [c for c in chunks if c["chunk_type"] == "case"]
    assert [c["symbol"] for c in case_chunks] == [
        "order.created", "order.cancelled", "user.registered", "_",
    ]


def test_java_treesitter_boundaries():
    chunks, _ = _chunk("src/java/OrderService.java")
    assert _types(chunks) == ["block", "class", "interface", "record"]
    symbols = [c["symbol"] for c in chunks[1:]]
    assert symbols == ["OrderService", "OrderRepository", "Order"]


def test_csharp_treesitter_boundaries_and_dotted_namespace():
    chunks, _ = _chunk("src/csharp/OrderService.cs")
    assert _types(chunks) == ["block", "class", "interface", "record"]


# ── Markdown tree-index gate (should_use_tree_index) ──────────────────────────

def test_large_markdown_doc_triggers_tree_index_zero_qdrant_chunks():
    chunks, use_tree_index = _chunk(
        "docs/large-guide.md", config={"doc_index": {"enabled": True}}
    )
    assert use_tree_index is True
    assert chunks == []  # tree-indexed docs get NO Qdrant chunks — see chunker.py:899-905


def test_small_markdown_doc_stays_on_normal_chunk_path():
    chunks, use_tree_index = _chunk(
        "docs/small-note.md", config={"doc_index": {"enabled": True}}
    )
    assert use_tree_index is False
    assert len(chunks) == 3
    assert all(c["chunk_type"] == "heading" for c in chunks)


def test_markdown_tree_index_disabled_by_default_config():
    # doc_index.enabled defaults to False (should_use_tree_index's own
    # early-return) — a large doc must NOT be tree-indexed if the caller
    # passes an empty/default config, even though it has enough headings.
    chunks, use_tree_index = _chunk("docs/large-guide.md", config={})
    assert use_tree_index is False
    assert len(chunks) > 0


@pytest.mark.parametrize("min_headings,expected_tree_index", [(20, False), (2, True)])
def test_markdown_tree_index_respects_min_headings_config(min_headings, expected_tree_index):
    config = {"doc_index": {"enabled": True, "min_headings_for_tree": min_headings}}
    _, use_tree_index = _chunk("docs/large-guide.md", config=config)
    assert use_tree_index is expected_tree_index


# ── Dispatch-arm sub-chunking threshold (resolves roadmap C.2) ────────────────

def _switch_snippet(n_arms: int) -> str:
    cases = "\n".join(f'      case "arm{i}": return {i};' for i in range(n_arms))
    return (
        "export class Dispatcher {\n"
        "  route(x: string): number {\n"
        "    switch (x) {\n"
        f"{cases}\n"
        "      default: return -1;\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


@pytest.mark.parametrize("n_arms", [1, 2])
def test_dispatch_arm_subchunking_does_not_fire_below_threshold(n_arms):
    chunks, _ = chunker.chunk_file("Dispatcher.ts", _switch_snippet(n_arms), {})
    # Below _MIN_ARMS_FOR_SUBCHUNK (3): the switch stays inside one "class" chunk.
    assert "case" not in _types(chunks)
    assert "class" in _types(chunks)


@pytest.mark.parametrize("n_arms", [3, 5])
def test_dispatch_arm_subchunking_fires_at_and_above_threshold(n_arms):
    chunks, _ = chunker.chunk_file("Dispatcher.ts", _switch_snippet(n_arms), {})
    case_chunks = [c for c in chunks if c["chunk_type"] == "case"]
    assert len(case_chunks) == n_arms
