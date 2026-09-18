"""Structure-preserving chunker (ticket #5, spec §12).

Fixed-size token chunking is never the only mechanism: chunks respect
section hierarchy, heading context, paragraph boundaries, table boundaries,
slide/page metadata, and stay traceable to source elements.
"""
from __future__ import annotations

from typing import Any

CHUNK_TARGET_CHARS = 1200
CHUNK_MAX_CHARS = 2400
MAX_TABLE_CHARS = 4000
ELEMENT_TYPES = ("heading", "paragraph", "table", "list_item", "slide", "sheet", "list")


def chunk_document(doc: dict, chunk_id_factory=None) -> list[dict]:
    """Walk canonical elements in reading order and emit chunk dicts."""
    chunks: list[dict] = []
    section_path: list[str] = []
    page: int | None = None
    buf_text: list[str] = []
    buf_elements: list[str] = []
    seq = 0

    new_id = chunk_id_factory or (lambda n: f"chk_{n:04d}")

    def flush():
        nonlocal buf_text, buf_elements, seq
        text = "\n\n".join(buf_text).strip()
        if text:
            seq += 1
            chunks.append(
                {
                    "chunk_id": new_id(seq),
                    "document_id": doc["document_id"],
                    "section_path": list(section_path),
                    "text": text,
                    "page": page,
                    "sequence": seq,
                    "source_element_ids": list(buf_elements),
                }
            )
        buf_text, buf_elements = [], []

    for index, el in enumerate(doc.get("elements", [])):
        el = dict(el)
        el.setdefault("id", f"el_{index + 1:04d}")
        etype = el.get("type", "paragraph")

        # page boundaries split chunks so page metadata stays accurate (§12)
        el_page = el.get("page")
        if el_page is not None and page is not None and el_page != page and buf_text:
            flush()
        if el_page is not None:
            page = el_page

        if etype == "heading":
            flush()
            level = el.get("level", 1)
            section_path = section_path[: level - 1]
            section_path.append(el.get("text", "").strip())
            continue

        text = (el.get("text") or "").strip()
        if not text:
            continue

        if etype == "table":
            flush()
            if len(text) > MAX_TABLE_CHARS:
                text = text[:MAX_TABLE_CHARS] + "\n… [table truncated]"
            sheet = (el.get("meta") or {}).get("sheet")
            seq += 1
            chunks.append(
                {
                    "chunk_id": new_id(seq),
                    "document_id": doc["document_id"],
                    "section_path": [sheet] if sheet else list(section_path),
                    "text": text,
                    "page": page,
                    "sequence": seq,
                    "source_element_ids": [el["id"]],
                }
            )
            continue

        if etype in ("slide", "sheet"):
            flush()
            section_path = [text]
            continue

        # paragraph / list_item: fill buffer, respect boundaries
        if sum(len(t) for t in buf_text) + len(text) > CHUNK_TARGET_CHARS and buf_text:
            flush()
        if len(text) > CHUNK_MAX_CHARS:
            # hard-oversize paragraph: split on sentence-ish boundaries
            piece, rest = text, ""
            while len(piece) > CHUNK_MAX_CHARS:
                cut = piece.rfind(". ", 0, CHUNK_MAX_CHARS)
                cut = cut + 1 if cut > CHUNK_MAX_CHARS // 2 else CHUNK_MAX_CHARS
                piece, rest = piece[:cut].strip(), piece[cut:].strip()
            buf_text.append(piece)
            buf_elements.append(el["id"])
            flush()
            if rest:
                buf_text, buf_elements = [rest], [el["id"]]
            continue
        buf_text.append(text)
        buf_elements.append(el["id"])

    flush()
    return chunks
