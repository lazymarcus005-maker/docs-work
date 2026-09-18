"""Canonical document representation (spec §10).

Downstream components depend only on this shape — never on the original
file format (§10.2). Serialized as JSON into parsed/<document-id>.json.
"""
from __future__ import annotations

from typing import Any

CANONICAL_VERSION = "canonical-1"

ELEMENT_TYPES = {
    "heading",
    "paragraph",
    "table",
    "table_row",
    "list",
    "list_item",
    "slide",
    "sheet",
    "image",
    "page",
}


def canonical_document(
    document_id: str,
    source: str,
    file_type: str,
    elements: list[dict],
) -> dict:
    ordered = []
    for i, el in enumerate(elements):
        el = dict(el)
        el.setdefault("type", "paragraph")
        el.setdefault("text", "")
        el["id"] = el.get("id") or f"el_{i + 1:04d}"
        el["reading_order"] = i
        ordered.append(el)
    return {
        "document_id": document_id,
        "source": source,
        "file_type": file_type,
        "canonical_version": CANONICAL_VERSION,
        "elements": ordered,
    }
