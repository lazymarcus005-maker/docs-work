# ADR-0001: SQLite (not DuckDB) as the default local database

## Status

Accepted (V1 implementation of spec §16).

## Context

The spec recommends DuckDB as the default lightweight local database and
explicitly allows "SQLite where appropriate" (§16). All persistence must
stay local, file-based, and CPU-only, with FTS for text search (§17) and a
DB-backed job queue (§37).

## Decision

We use SQLite with WAL mode and FTS5 as the V1 store, behind `app/db.py`
and repository helpers. Nothing outside those modules talks SQL directly
to the engine.

## Consequences

- Zero extra dependencies (Python stdlib), maximum portability, and
  battle-tested FTS5 — the whole app runs from a fresh clone quickly.
- DuckDB remains a drop-in candidate behind the same seam (NFR-004) if
  analytical queries over large chunk tables become a bottleneck.
- Single-writer concurrency is mitigated with WAL + busy timeouts; job
  workers use their own connections.
