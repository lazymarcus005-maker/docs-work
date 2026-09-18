"""Document processing pipeline (ticket #5): parse → normalize → chunk →
index. Each source moves NEW → PARSING → PARSED → INDEXING → READY/FAILED
(spec §11). Callable synchronously or via the job queue."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..config import Settings
from ..storage import filesystem as fs
from ..util import log_event, now_iso
from . import parser as parser_mod
from .chunker import chunk_document

EXTRACTION_VERSION = "rule-1"


def mark_stale(conn: sqlite3.Connection) -> int:
    """Flag documents whose stored processing versions no longer match the
    current configuration — they will be reprocessed on demand (§11, §61)."""
    current_parser = parser_mod.PARSER_VERSION
    current_extraction = EXTRACTION_VERSION
    cur = conn.execute(
        "UPDATE documents SET status = 'STALE', updated_at = ?"
        " WHERE status = 'READY' AND (parser_version != ? OR extraction_version != ?)",
        (now_iso(), current_parser, current_extraction),
    )
    conn.commit()
    return cur.rowcount


def _set_doc_status(conn: sqlite3.Connection, doc_id: str, status: str,
                    error: str | None = None) -> None:
    conn.execute(
        "UPDATE documents SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, error, now_iso(), doc_id),
    )
    conn.commit()


def process_document(conn: sqlite3.Connection, settings: Settings, document_id: str) -> dict:
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if doc is None:
        raise ValueError(f"document {document_id} not found")
    doc = dict(doc)
    project_id = doc["project_id"]
    src = fs.safe_join(
        fs.context_dir(settings.workspace_root, project_id), doc["stored_name"]
    )

    # ------------------------------------------------------------- parse
    _set_doc_status(conn, document_id, "PARSING")
    try:
        canonical = parser_mod.parse_file(document_id, src, doc["kind"])
    except ValueError as e:
        msg = f"{doc['name']} could not be parsed.\nReason: {e}\nActions: [Retry]"
        _set_doc_status(conn, document_id, "FAILED", error=msg)
        log_event(conn, "document.failed", project_id, {"file": doc["name"]})
        conn.commit()
        return {"status": "FAILED", "error": msg}

    needs_ocr = any(
        (el.get("meta") or {}).get("needs_ocr")
        for el in canonical["elements"]
        if el["type"] == "image"
    )
    text_elements = [el for el in canonical["elements"] if el["type"] != "image"]
    if needs_ocr:
        from . import ocr as ocr_mod

        engine_row = conn.execute(
            "SELECT value FROM settings WHERE key='ocr_engine'").fetchone()
        engine = engine_row["value"] if engine_row else "tesseract"
        if ocr_mod.ocr_enabled(conn):
            # §34: OCR activates only when needed; run it and re-parse the text
            try:
                ocr_text = ocr_mod.ocr_document(src, engine)
            except ocr_mod.OCRError as e:
                msg = f"{doc['name']} could not be parsed.\nReason: {e}"
                _set_doc_status(conn, document_id, "FAILED", error=msg)
                conn.commit()
                return {"status": "FAILED", "error": msg, "needs_ocr": True}
            if ocr_text.strip():
                canonical["elements"] = [
                    {"type": "paragraph", "text": para.strip()}
                    for para in ocr_text.split("\n\n") if para.strip()
                ]
                text_elements = canonical["elements"]
        elif not text_elements:
            msg = (
                f"{doc['name']} could not be parsed.\n"
                "Reason: The document contains scanned pages / images and OCR is disabled.\n"
                "Actions: [Enable OCR] [Retry]"
            )
            _set_doc_status(conn, document_id, "FAILED", error=msg)
            log_event(conn, "document.failed", project_id, {"file": doc["name"], "needs_ocr": True})
            conn.commit()
            return {"status": "FAILED", "error": msg, "needs_ocr": True}

    parsed_dir = fs.parsed_dir(settings.workspace_root, project_id)
    parsed_dir.mkdir(parents=True, exist_ok=True)
    (parsed_dir / f"{document_id}.json").write_text(
        json.dumps(canonical, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    conn.execute(
        "UPDATE documents SET parser_version = ?, updated_at = ? WHERE id = ?",
        (parser_mod.PARSER_VERSION, now_iso(), document_id),
    )
    conn.commit()
    _set_doc_status(conn, document_id, "PARSED")
    log_event(conn, "document.parsed", project_id, {"file": doc["name"]})

    # --------------------------------------------------- chunk + index
    _set_doc_status(conn, document_id, "INDEXING")
    chunks = chunk_document(canonical, chunk_id_factory=lambda n: f"chk_{document_id[-6:]}_{n:04d}")
    _replace_chunks(conn, document_id, project_id, chunks)
    conn.execute(
        "UPDATE documents SET status = 'READY', extraction_version = ?,"
        " last_processed_at = ?, error = NULL, updated_at = ? WHERE id = ?",
        (EXTRACTION_VERSION, now_iso(), now_iso(), document_id),
    )
    log_event(conn, "index.updated", project_id,
              {"file": doc["name"], "chunks": len(chunks)})
    conn.commit()
    return {"status": "READY", "chunks": len(chunks)}


def _replace_chunks(
    conn: sqlite3.Connection, document_id: str, project_id: str, chunks: list[dict]
) -> None:
    old = [r["id"] for r in conn.execute(
        "SELECT id FROM chunks WHERE document_id = ?", (document_id,)
    ).fetchall()]
    for cid in old:
        conn.execute("DELETE FROM embeddings WHERE chunk_id = ?", (cid,))
        conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (cid,))
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))

    ts = now_iso()
    for c in chunks:
        conn.execute(
            "INSERT INTO chunks (id, document_id, project_id, section_path, text,"
            " page, sequence, source_element_ids, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                c["chunk_id"], document_id, project_id,
                json.dumps(c["section_path"], ensure_ascii=False),
                c["text"], c["page"], c["sequence"],
                json.dumps(c["source_element_ids"]), ts,
            ),
        )
        conn.execute(
            "INSERT INTO chunks_fts (chunk_id, text, section_path) VALUES (?, ?, ?)",
            (c["chunk_id"], c["text"],
             json.dumps(c["section_path"], ensure_ascii=False)),
        )


def parse_document_task(conn: sqlite3.Connection, settings: Settings, job: dict) -> None:
    """Job-queue handler for PARSE_DOCUMENT."""
    document_id = job.get("document_id") or job["payload"].get("document_id")
    result = process_document(conn, settings, document_id)
    if result["status"] == "FAILED":
        raise RuntimeError(result["error"])

    # enrichment runs at lower priority than interactive work (spec §38)
    from ..jobs import queue

    queue.enqueue(
        conn, job["project_id"], "EMBED_DOCUMENT",
        document_id=document_id, priority=queue.PRIORITY_PARSE + 10,
    )
    queue.enqueue(
        conn, job["project_id"], "EXTRACT_ENTITIES",
        document_id=document_id, priority=queue.PRIORITY_ENRICHMENT,
    )


def embed_document_task(conn: sqlite3.Connection, settings: Settings, job: dict) -> None:
    """Job-queue handler for EMBED_DOCUMENT (ticket #14)."""
    from ..embeddings import provider as emb_provider
    from ..retrieval import vector_search

    document_id = job.get("document_id") or job["payload"].get("document_id")
    provider = emb_provider.get_provider(settings.embedding_model)
    vector_search.embed_document_chunks(conn, provider, document_id)
    conn.execute(
        "UPDATE documents SET embedding_version = ?, updated_at = ? WHERE id = ?",
        (provider.model, now_iso(), document_id),
    )
    conn.commit()
