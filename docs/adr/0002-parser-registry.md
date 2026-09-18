# ADR-0002: Built-in CPU parsers first, Docling preferred when installed

## Status

Accepted (V1 implementation of spec §10.1).

## Context

The spec recommends Docling as the default parser but requires parsing to
be local, deterministic, CPU-only, and behind a replaceable interface
(§10, NFR-004, NFR-007). Docling brings heavy model downloads at first
parse time.

## Decision

`app/documents/parser.py` defines the canonical-document contract and a
parser registry. Lightweight built-in parsers (pypdf, python-docx,
python-pptx, openpyxl, csv, native Markdown/TXT) are always available
offline. If the `docling` package is importable, it is preferred
automatically for PDF/DOCX/PPTX. New formats register into the same
registry (§8 extensibility).

## Consequences

- Fresh installs parse immediately with no model downloads; V1 quality on
  complex layouts depends on the lighter parsers until Docling is added.
- The canonical document representation is the only downstream contract,
  so swapping parser implementations cannot ripple into retrieval,
  knowledge, or the agent.
