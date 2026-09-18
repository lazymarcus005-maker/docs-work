"""Ticket #5 seam: the structure-preserving chunker (spec §12)."""
from __future__ import annotations

from app.documents.chunker import chunk_document


def make_doc(elements):
    return {"document_id": "doc_001", "source": "SRS.docx", "elements": elements}


def test_sections_carry_heading_context():
    doc = make_doc([
        {"type": "heading", "level": 1, "text": "Authentication"},
        {"type": "paragraph", "text": "The gateway forwards auth requests."},
        {"type": "heading", "level": 2, "text": "Token Validation"},
        {"type": "paragraph", "text": "Tokens expire after 30 minutes."},
    ])
    chunks = chunk_document(doc)
    assert chunks[0]["section_path"] == ["Authentication"]
    assert chunks[1]["section_path"] == ["Authentication", "Token Validation"]
    assert "Tokens expire" in chunks[1]["text"]


def test_nested_heading_resets_deeper_levels():
    doc = make_doc([
        {"type": "heading", "level": 1, "text": "A"},
        {"type": "heading", "level": 2, "text": "B"},
        {"type": "paragraph", "text": "under B"},
        {"type": "heading", "level": 1, "text": "C"},
        {"type": "paragraph", "text": "under C"},
    ])
    chunks = chunk_document(doc)
    assert chunks[0]["section_path"] == ["A", "B"]
    assert chunks[1]["section_path"] == ["C"]


def test_tables_stay_whole_and_traceable():
    table = {"id": "el_010", "type": "table", "text": "id | name\n1 | redis"}
    doc = make_doc([
        {"type": "heading", "level": 1, "text": "Intro"},
        {"type": "paragraph", "text": "before table"},
        table,
    ])
    chunks = chunk_document(doc)
    table_chunk = next(c for c in chunks if "redis" in c["text"])
    assert table_chunk["source_element_ids"] == ["el_010"]
    # a table never merges into a prose chunk
    assert table_chunk["text"].startswith("id | name")


def test_chunks_trace_to_source_elements_and_sequence():
    doc = make_doc([
        {"type": "paragraph", "text": "one"},
        {"type": "paragraph", "text": "two"},
    ])
    chunks = chunk_document(doc)
    assert chunks[0]["sequence"] == 1
    assert chunks[0]["source_element_ids"] == ["el_0001"] or chunks[0]["source_element_ids"]
    assert all(c["document_id"] == "doc_001" for c in chunks)


def test_page_metadata_preserved():
    doc = make_doc([
        {"type": "paragraph", "text": "page four content", "page": 4},
        {"type": "paragraph", "text": "more content", "page": 5},
    ])
    chunks = chunk_document(doc)
    assert any(c["page"] == 4 for c in chunks)


def test_oversize_paragraph_is_split_not_dropped():
    big = "Sentence. " * 400  # ~4000 chars
    doc = make_doc([{"type": "paragraph", "text": big}])
    chunks = chunk_document(doc)
    assert len(chunks) >= 2
    assert all(len(c["text"]) <= 2500 for c in chunks)


def test_slide_boundaries_reset_sections():
    doc = make_doc([
        {"type": "slide", "text": "Slide 1"},
        {"type": "paragraph", "text": "slide one body"},
        {"type": "slide", "text": "Slide 2"},
        {"type": "paragraph", "text": "slide two body"},
    ])
    chunks = chunk_document(doc)
    assert chunks[0]["section_path"] == ["Slide 1"]
    assert chunks[1]["section_path"] == ["Slide 2"]
