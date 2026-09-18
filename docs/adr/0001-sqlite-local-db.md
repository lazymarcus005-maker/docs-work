# ADR-0001: SQLite (not DuckDB) as the default local database

## Status

Accepted (V1 implementation of spec §16).

## Context

The spec recommends DuckDB as the default lightweight local database and
explicitly allows "SQLite where appropriate" (§16). All persistence must
stay local, file-based, and CPU-only, with FTS for text search (§17) and a
DB-backed job queue (§37).

## Decision

We use SQLite with WAL mode and FTS5 as the V1 store. `app/db.py` owns the
schema and the connection seam; engine-specific SQL (FTS5 virtual tables,
bm25 ranking) is isolated in the retrieval and storage modules. Domain
modules execute parameterized SQL against that seam rather than through a
full repository layer — acceptable for V1's size, with the honest
limitation that a database swap would touch more than one module. A
repository-layer extraction is the designated follow-up if a second engine
(DuckDB for analytical queries) becomes real (NFR-004).

## Consequences

- Zero extra dependencies (Python stdlib), maximum portability, and
  battle-tested FTS5 — the whole app runs from a fresh clone quickly.
- Single-writer concurrency is mitigated with WAL + busy timeouts; job
  workers use their own connections.
- The replaceability claim is scoped to the connection/schema seam and the
  FTS isolation, not the whole data-access surface — see follow-up above.
