"""Rule/dictionary entity + relation extraction (ticket #12, spec §13).

CPU-first staged extraction: dictionary matching and deterministic patterns
do the baseline; the LLM is never required per chunk (§13, NFR-007).
Deterministic output keeps tests and the review queue meaningful.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TECHNOLOGY_DICTIONARY = {
    "redis", "postgresql", "postgres", "mysql", "mongodb", "kafka", "docker",
    "kubernetes", "nginx", "rabbitmq", "elasticsearch", "sqlite", "duckdb",
    "grpc", "graphql", "oauth", "oidc", "saml", "jwt",
}

SERVICE_SUFFIX_RE = re.compile(
    r"\b[a-z0-9]+(gateway|service|svc|app|ux|api|worker|proxy|daemon|server|core|hub)s?\b",
    re.IGNORECASE,
)
CAMEL_RE = re.compile(r"\b([A-Z][a-z0-9]+){2,}\b")
API_PATH_RE = re.compile(r"/(api/)?[a-z0-9_\-/]+", re.IGNORECASE)
REQ_ID_RE = re.compile(r"\b(?:REQ|NFR|FR|US)-\d+\b")

RELATION_PATTERNS = [
    (re.compile(r"\bforwards?\b[^.]*?\bto\b", re.I), "CALLS", 0.9),
    (re.compile(r"\broute[sd]?\b[^.]*?\bto\b", re.I), "CALLS", 0.85),
    (re.compile(r"\bcalls?\b", re.I), "CALLS", 0.9),
    (re.compile(r"\buses?\b", re.I), "USES", 0.85),
    (re.compile(r"\bdepends? on\b", re.I), "DEPENDS_ON", 0.9),
    (re.compile(r"\bstores?\b[^.]*?\bin\b", re.I), "CONSUMES", 0.8),
    (re.compile(r"\b(cached|kept|persisted)\b[^.]*?\bin\b", re.I), "CONSUMES", 0.8),
    (re.compile(r"\bproduces?\b", re.I), "PRODUCES", 0.85),
    (re.compile(r"\bimplements?\b", re.I), "IMPLEMENTS", 0.85),
]

CONFLICT_PATTERNS = [
    (re.compile(r"\bexpires? after (\d+) minutes\b", re.I), "token_lifetime_minutes"),
]

EXTRACTION_VERSION = "rule-1"


def extract_entities_task(conn, settings, job) -> None:  # noqa: ANN001 — job handler
    """Background knowledge extraction for one document (low priority,
    spec §38). Produces evidence-backed entities/relations and conflict
    records; uncertain items go to the review queue, nothing merges
    silently."""
    import json as _json

    from ..util import log_event, new_id, now_iso
    from . import resolver

    document_id = job.get("document_id") or job["payload"].get("document_id")
    project_id = job["project_id"]
    doc = conn.execute("SELECT name FROM documents WHERE id = ?", (document_id,)).fetchone()
    if doc is None:
        return

    ts = now_iso()
    for chunk in conn.execute(
        "SELECT id, text, page FROM chunks WHERE document_id = ? ORDER BY sequence",
        (document_id,),
    ).fetchall():
        result = extract_from_text(chunk["text"])

        resolved: dict[str, str] = {}
        for mention in result.mentions:
            eid, _created = resolver.find_or_create_entity(
                conn, project_id, mention.name, mention.type,
                source_document_id=document_id,
            )
            resolved[mention.name] = eid

        seen_relations = set()
        for cand in result.relations:
            src = resolved.get(cand.source)
            dst = resolved.get(cand.target)
            if not src or not dst or src == dst:
                continue
            key = (src, cand.relation_type, dst)
            if key in seen_relations:
                continue
            seen_relations.add(key)
            rel_id = new_id("rel")
            try:
                conn.execute(
                    "INSERT INTO relations (id, project_id, source_entity_id,"
                    " relation_type, target_entity_id, confidence, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                    (rel_id, project_id, src, cand.relation_type, dst,
                     cand.confidence, ts),
                )
            except Exception:
                continue  # duplicate relation — evidence appends below
            if cand.confidence < 0.6:
                conn.execute(
                    "INSERT INTO review_items (id, project_id, kind, payload,"
                    " suggestion, status, created_at) VALUES (?, ?,"
                    " 'low_confidence_relation', ?, ?, 'open', ?)",
                    (new_id("rev"), project_id,
                     _json.dumps({"relation_id": rel_id,
                                  "relation": f"{cand.source} --{cand.relation_type}--> {cand.target}",
                                  "confidence": cand.confidence}),
                     _json.dumps({"action": "confirm"}), ts),
                )
            conn.execute(
                "INSERT INTO relation_evidence (id, relation_id, document_id,"
                " chunk_id, page, text) VALUES (?, ?, ?, ?, ?, ?)",
                (new_id("rev"), rel_id, document_id, chunk["id"], chunk["page"],
                 chunk["text"][:400]),
            )

        for conflict in result.conflicts:
            existing = conn.execute(
                "SELECT id, details FROM conflicts WHERE project_id = ? AND subject = ?",
                (project_id, conflict["subject"]),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO conflicts (id, project_id, subject, details, status, created_at)"
                    " VALUES (?, ?, ?, ?, 'open', ?)",
                    (new_id("cfl"), project_id, conflict["subject"],
                     _json.dumps([{"document": doc["name"], "value": conflict["value"]}]),
                     ts),
                )
            else:
                details = _json.loads(existing["details"] or "[]")
                if not any(d.get("value") == conflict["value"] for d in details):
                    details.append({"document": doc["name"], "value": conflict["value"]})
                    conn.execute(
                        "UPDATE conflicts SET details = ? WHERE id = ?",
                        (_json.dumps(details), existing["id"]),
                    )
                    if len(details) > 1:
                        conn.execute(
                            "INSERT INTO review_items (id, project_id, kind, payload,"
                            " suggestion, status, created_at) VALUES (?, ?, 'conflict',"
                            " ?, ?, 'open', ?)",
                            (new_id("rev"), project_id,
                             _json.dumps({"subject": conflict["subject"],
                                          "values": details}),
                             _json.dumps({"action": "review"}), ts),
                        )

    conn.execute(
        "UPDATE documents SET extraction_version = ?, updated_at = ? WHERE id = ?",
        (EXTRACTION_VERSION, now_iso(), document_id),
    )
    log_event(conn, "entity.created", project_id, {"document": doc["name"]})
    conn.commit()


@dataclass
class Mention:
    name: str
    type: str


@dataclass
class RelationCandidate:
    source: str
    relation_type: str
    target: str
    confidence: float


@dataclass
class ChunkExtraction:
    mentions: list[Mention] = field(default_factory=list)
    relations: list[RelationCandidate] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def extract_from_text(text: str) -> ChunkExtraction:
    """Stage 1+2: dictionary matching and deterministic patterns."""
    out = ChunkExtraction()
    seen: set[tuple[str, str]] = set()

    def add(name: str, etype: str):
        key = (normalize_name(name), etype)
        if name and key not in seen and len(name) > 1:
            seen.add(key)
            out.mentions.append(Mention(name, etype))

    for token in re.findall(r"[A-Za-z0-9_\-./]+", text):
        low = token.lower().strip("./-")
        if low in TECHNOLOGY_DICTIONARY:
            add(low, "Technology")
    for m in SERVICE_SUFFIX_RE.finditer(text):
        add(m.group(0), "Service")
    for m in CAMEL_RE.finditer(text):
        add(m.group(0), "Service")
    for m in REQ_ID_RE.finditer(text):
        add(m.group(0), "Requirement")

    # relations: sentences that contain >= 2 distinct mentions + a pattern
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        local = [men for men in out.mentions
                 if re.search(rf"\b{re.escape(men.name)}\b", sentence, re.IGNORECASE)]
        if len(local) < 2:
            continue
        matched = False
        for pat, rel_type, confidence in RELATION_PATTERNS:
            if pat.search(sentence):
                for a, b in zip(local, local[1:]):
                    out.relations.append(RelationCandidate(
                        a.name, rel_type, b.name, confidence))
                    matched = True
                break
        if not matched and len(local) >= 2:
            for a, b in zip(local, local[1:]):
                out.relations.append(RelationCandidate(a.name, "RELATED_TO", b.name, 0.4))

    for pat, subject in CONFLICT_PATTERNS:
        m = pat.search(text)
        if m:
            out.conflicts.append({"subject": subject, "value": int(m.group(1))})

    return out
