"""Embedding layer (ticket #14, spec §33).

Embeddings run independently from the chat LLM. The default provider is a
deterministic, CPU-only feature-hashing embedder (no downloads, works
offline); if sentence-transformers is installed it is used automatically.
Vectors are cached in the local DB and only recomputed when content,
chunking, model, or configuration changed.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

DIM = 256


class EmbeddingProvider(Protocol):
    model: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic lexical embedding via feature hashing. CPU-only and
    dependency-free — the guaranteed offline baseline."""

    model = "builtin-hash-256"
    dim = DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            tokens = re.findall(r"[a-z0-9]+", text.lower())
            for token in tokens:
                digest = hashlib.md5(token.encode()).digest()
                idx = int.from_bytes(digest[:4], "little") % self.dim
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vec[idx] += sign
                # bigram-ish context: hash pairs of consecutive tokens
            for i in range(len(tokens) - 1):
                digest = hashlib.md5(f"{tokens[i]}_{tokens[i+1]}".encode()).digest()
                idx = int.from_bytes(digest[:4], "little") % self.dim
                vec[idx] += 0.5 if digest[4] % 2 == 0 else -0.5
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class SentenceTransformerEmbedder:
    """Used automatically when sentence-transformers is installed (CPU)."""

    def __init__(self, model_name: str = "paraphrase-MiniLM-L3-v2") -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore

        self._model = SentenceTransformer(model_name, device="cpu")
        self.model = f"st:{model_name}"
        self.dim = self._model.get_sentence_embedding_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, show_progress_bar=False)
        return [[float(x) for x in v] for v in vectors]


def get_provider(model_name: str | None = None) -> EmbeddingProvider:
    try:
        return SentenceTransformerEmbedder()
    except Exception:  # noqa: BLE001 — dependency is optional
        return HashingEmbedder()


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
