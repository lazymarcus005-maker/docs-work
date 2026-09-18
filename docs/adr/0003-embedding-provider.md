# ADR-0003: Deterministic hashing embedder as the offline baseline

## Status

Accepted (V1 implementation of spec §33).

## Context

Embeddings must run on CPU, offline, cached, and independently of the chat
LLM (§33, FR-014/15). sentence-transformers improves semantics but pulls
in torch (large download, first-run model fetch).

## Decision

`app/embeddings/provider.py` prefers sentence-transformers (CPU) when the
package is installed; otherwise it falls back to a deterministic
feature-hashing embedder (256-dim, lexical, zero dependencies). Vector
search and hybrid ranking work identically against either provider, and
re-embedding follows the §33 cache rules (content/chunking/model/config
change only).

## Consequences

- Guaranteed offline, fast startup; hybrid retrieval degrades to lexical
  similarity until a semantic model is installed.
- The provider interface keeps swapping in real semantic models a config
  change, not a code change.
