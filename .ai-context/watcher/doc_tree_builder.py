"""
doc_tree_builder.py
PageIndex-style hierarchical tree builder for long-form Markdown documentation files.

Each node represents a heading section and carries:
  - node_id      : zero-padded sequential ID
  - title        : heading text (without # prefix)
  - start_line   : 1-based start line
  - end_line     : 1-based end line (inclusive)
  - summary      : LLM-generated summary of the section (null if LLM unavailable)
  - body_text    : verbatim text of the section (from start_line to end_line),
                   kept in the stored JSON so doc-tree retrieval never needs
                   to read the file from disk.
  - children     : list of child nodes (deeper heading levels)

root level also carries:
  - doc_description : one-sentence summary of the whole document
  - node_count      : total node count (flattened)

_raw_text is used during building for LLM summarisation; it is renamed to
body_text before storage so the verbatim section text remains available.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)", re.MULTILINE)


# ── Heading parser ─────────────────────────────────────────────────────────────

def _parse_headings(text: str) -> list[dict]:
    """Return a list of {level, title, start_line, end_line, raw_text} dicts."""
    lines = text.splitlines()
    source = "\n".join(lines)
    matches = list(_MD_HEADING.finditer(source))
    if not matches:
        return []

    headings = []
    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()
        start_line = source[: m.start()].count("\n") + 1  # 1-based
        end_line = (
            source[: matches[i + 1].start()].count("\n")  # line before next heading
            if i + 1 < len(matches)
            else len(lines)
        )
        # raw_text: from this heading's line to just before the next heading
        raw_text = "\n".join(lines[start_line - 1 : end_line])
        headings.append({
            "level":      level,
            "title":      title,
            "start_line": start_line,
            "end_line":   end_line,
            "raw_text":   raw_text,
        })
    return headings


# ── Tree builder ───────────────────────────────────────────────────────────────

def _build_tree_structure(headings: list[dict]) -> list[dict]:
    """Convert flat heading list into a nested tree of nodes (body_text included)."""
    counter = [0]

    def make_node(h: dict) -> dict:
        counter[0] += 1
        return {
            "node_id":    f"{counter[0]:04d}",
            "title":      h["title"],
            "start_line": h["start_line"],
            "end_line":   h["end_line"],
            "summary":    None,
            "body_text":  h["raw_text"],   # kept for diskless retrieval
            "_level":     h["level"],
            "children":   [],
        }

    roots: list[dict] = []
    stack: list[dict] = []

    for h in headings:
        node = make_node(h)
        # Pop stack until we find a node with a shallower level
        while stack and stack[-1]["_level"] >= node["_level"]:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            roots.append(node)
        stack.append(node)

    return roots


def _count_nodes(nodes: list[dict]) -> int:
    total = 0
    for n in nodes:
        total += 1
        total += _count_nodes(n.get("children", []))
    return total


def _strip_internal_fields(nodes: list[dict]) -> None:
    """Remove internal-only fields from all nodes before DB storage."""
    for n in nodes:
        n.pop("_level", None)
        _strip_internal_fields(n.get("children", []))


# ── LLM summarisation ─────────────────────────────────────────────────────────

def _summarise_nodes(nodes: list[dict], llm_client: Any, max_tokens: int) -> None:
    """Recursively summarise each node using the LLM. Modifies nodes in-place."""
    for node in nodes:
        raw = node.get("body_text", "")
        if raw:
            try:
                node["summary"] = _call_llm_summary(raw, llm_client, max_tokens)
            except Exception as e:
                log.warning(f"LLM summarisation failed for node '{node['title']}': {e}")
                node["summary"] = None
        _summarise_nodes(node.get("children", []), llm_client, max_tokens)


def _call_llm_summary(text: str, llm_client: Any, max_tokens: int) -> str:
    """Call LLM and return a summary string for the given text excerpt."""
    prompt = (
        "Summarise the following documentation section in 1–2 sentences. "
        "Be concise and specific — include key concepts, APIs, or decisions mentioned.\n\n"
        f"---\n{text[:4000]}\n---"
    )
    response = llm_client.chat_completion(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.1,
    )
    return response.strip()


def _doc_description(text: str, llm_client: Any, max_tokens: int) -> str | None:
    """Generate a one-sentence description for the whole document."""
    excerpt = text[:3000]
    prompt = (
        "In one sentence, describe what this documentation file is about. "
        "Be specific and technical.\n\n"
        f"---\n{excerpt}\n---"
    )
    try:
        return llm_client.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.1,
        ).strip()
    except Exception as e:
        log.warning(f"Doc description LLM call failed: {e}")
        return None


# ── Public API ─────────────────────────────────────────────────────────────────

def build_tree_for_file(
    text: str,
    file_path: str,
    llm_client: Any | None,
    config: dict,
) -> dict:
    """
    Parse the Markdown text and build a PageIndex-style hierarchical tree.
    Returns a dict ready for DB storage (raw_text stripped).

    {
      "file_path":        str,
      "doc_description":  str | None,
      "node_count":       int,
      "nodes":            list[dict]   # hierarchical, body_text preserved
    }
    """
    doc_cfg   = config.get("doc_index", {})
    max_tok   = doc_cfg.get("max_tokens_per_node_summary", 300)

    headings = _parse_headings(text)
    if not headings:
        log.debug(f"doc_tree: no headings found in {file_path} — building empty tree")
        return {"file_path": file_path, "doc_description": None, "node_count": 0, "nodes": []}

    nodes = _build_tree_structure(headings)
    node_count = _count_nodes(nodes)

    description = None
    if llm_client is not None:
        description = _doc_description(text, llm_client, max_tok)
        _summarise_nodes(nodes, llm_client, max_tok)
    else:
        log.debug(f"doc_tree: LLM unavailable — tree built without summaries for {file_path}")

    _strip_internal_fields(nodes)
    log.info(f"doc_tree: built {node_count} nodes for {file_path}")

    return {
        "file_path":       file_path,
        "doc_description": description,
        "node_count":      node_count,
        "nodes":           nodes,
    }


def rebuild_summaries(
    tree_json: dict,
    text: str,
    llm_client: Any,
    config: dict,
) -> dict:
    """
    Backfill summaries for nodes that are null (LLM was unavailable on first build).
    Returns an updated tree_json dict.
    """
    doc_cfg  = config.get("doc_index", {})
    max_tok  = doc_cfg.get("max_tokens_per_node_summary", 300)
    headings = _parse_headings(text)

    # Build a lookup: node_id → raw_text, by re-parsing headings
    heading_map: dict[str, str] = {}
    counter = [0]
    for h in headings:
        counter[0] += 1
        heading_map[f"{counter[0]:04d}"] = h["raw_text"]

    def _fill(nodes: list[dict]) -> None:
        for n in nodes:
            if n.get("summary") is None:
                raw = heading_map.get(n["node_id"], "")
                if raw:
                    try:
                        n["summary"] = _call_llm_summary(raw, llm_client, max_tok)
                    except Exception as e:
                        log.warning(f"Backfill summary failed for node {n['node_id']}: {e}")
            _fill(n.get("children", []))

    nodes = tree_json.get("nodes", [])
    _fill(nodes)

    if tree_json.get("doc_description") is None:
        tree_json["doc_description"] = _doc_description(text, llm_client, max_tok)

    return tree_json

