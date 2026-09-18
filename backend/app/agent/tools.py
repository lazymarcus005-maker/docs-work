"""Agent tools (ticket #7, spec §20).

Every tool enforces the project boundary: an agent can only see data
belonging to its own project (NFR-006). Skills (ticket #8) must declare
the tools they use; the registry is the single permission surface.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Settings
from ..llm.base import ToolDef
from ..retrieval import text_search


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    settings: Settings
    project_id: str
    selected_document_ids: list[str] = field(default_factory=list)
    skill_id: str | None = None
    skill_version: str | None = None
    session_id: str | None = None
    run_id: str | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[[ToolContext, dict], dict]

    def defn(self) -> ToolDef:
        return ToolDef(self.name, self.description, self.parameters)


_REGISTRY: dict[str, Tool] = {}


def register(name: str, description: str, parameters: dict):
    def deco(fn):
        _REGISTRY[name] = Tool(name, description, parameters, fn)
        return fn

    return deco


def get_tools(names: list[str] | None = None) -> list[Tool]:
    if names is None:
        return list(_REGISTRY.values())
    return [_REGISTRY[n] for n in names if n in _REGISTRY]


def tool_defnames(names: list[str] | None) -> list[ToolDef]:
    return [t.defn() for t in get_tools(names)]


def execute(ctx: ToolContext, name: str, args: dict) -> dict:
    tool = _REGISTRY.get(name)
    if tool is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return tool.fn(ctx, args or {})
    except Exception as e:  # noqa: BLE001 — tool errors become observations
        return {"error": f"tool {name} failed: {e}"}


# ------------------------------------------------------------------ tools
@register(
    "list_project_files",
    "List the source documents in the current project with their status.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def list_project_files(ctx: ToolContext, args: dict) -> dict:
    rows = ctx.conn.execute(
        "SELECT id, name, kind, status FROM documents WHERE project_id = ?"
        " ORDER BY created_at",
        (ctx.project_id,),
    ).fetchall()
    return {"files": [dict(r) for r in rows]}


@register(
    "read_document",
    "Read the parsed content of a project document by name (returns text with"
    " section headings and page numbers).",
    {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    },
)
def read_document(ctx: ToolContext, args: dict) -> dict:
    row = ctx.conn.execute(
        "SELECT id FROM documents WHERE project_id = ? AND name = ?",
        (ctx.project_id, args["name"]),
    ).fetchone()
    if row is None:
        return {"error": f"document not found in this project: {args['name']}"}
    doc_id = row["id"]
    chunks = ctx.conn.execute(
        "SELECT id, section_path, text, page FROM chunks WHERE document_id = ?"
        " ORDER BY sequence",
        (doc_id,),
    ).fetchall()
    if not chunks:
        return {"document": args["name"], "status": "not_indexed_yet"}
    content = []
    for c in chunks:
        section = " > ".join(json.loads(c["section_path"] or "[]"))
        prefix = f"[{c['id']}|p{c['page']}]{' ' + section if section else ''}: "
        content.append(prefix + c["text"])
    return {
        "document": args["name"],
        "document_id": doc_id,
        "content": "\n\n".join(content),
    }


@register(
    "search_project",
    "Search all project context (documents, and knowledge when available) for"
    " relevant passages. Prefer this before asking the user for information.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)
def search_project(ctx: ToolContext, args: dict) -> dict:
    results = text_search.text_search(
        ctx.conn, ctx.project_id, args["query"],
        limit=int(args.get("limit", 10)),
        document_ids=ctx.selected_document_ids or None,
    )
    return {"results": results}


@register(
    "search_chunks",
    "Search the project's indexed text chunks (raw traceable passages).",
    {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)
def search_chunks(ctx: ToolContext, args: dict) -> dict:
    results = text_search.text_search(
        ctx.conn, ctx.project_id, args["query"], limit=10,
        document_ids=ctx.selected_document_ids or None,
    )
    return {"chunks": results}


@register(
    "get_evidence",
    "Get the source evidence (document, page, text) behind a chunk id that"
    " appears in project context or a generated artifact.",
    {
        "type": "object",
        "properties": {"chunk_id": {"type": "string"}},
        "required": ["chunk_id"],
        "additionalProperties": False,
    },
)
def get_evidence(ctx: ToolContext, args: dict) -> dict:
    import json as _json

    row = ctx.conn.execute(
        "SELECT c.id AS chunk_id, c.page, c.section_path, c.text,"
        " d.name AS document FROM chunks c"
        " JOIN documents d ON d.id = c.document_id"
        " WHERE c.id = ? AND c.project_id = ?",
        (args["chunk_id"], ctx.project_id),
    ).fetchone()
    if row is None:
        return {"error": f"no evidence for {args['chunk_id']} in this project"}
    out = dict(row)
    out["section_path"] = _json.loads(out["section_path"] or "[]")
    return out


# --------------------------------------------- memory tools (AI memory)
@register(
    "remember",
    "Save a durable project memory for future sessions: an important fact,"
    " decision, preference, or glossary term the user stated or that the"
    " conversation established. Use sparingly for durable things, not chat",
    {
        "type": "object",
        "properties": {
            "content": {"type": "string", "maxLength": 600},
            "kind": {
                "type": "string",
                "enum": ["fact", "decision", "preference", "glossary", "todo"],
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    },
)
def remember(ctx: ToolContext, args: dict) -> dict:
    from ..knowledge import memory

    try:
        item = memory.create_memory(
            ctx.conn, ctx.project_id,
            content=args["content"],
            kind=args.get("kind", "fact"),
            source="agent",
            source_refs={"session_id": ctx.session_id, "run_id": ctx.run_id},
        )
        return {"memory_id": item["id"], "content": item["content"],
                "deduplicated": item["created_at"] != item["updated_at"]}
    except memory.MemoryError as e:
        return {"error": str(e)}


@register(
    "list_memories",
    "List the durable project memories currently active.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def list_memories_tool(ctx: ToolContext, args: dict) -> dict:
    from ..knowledge import memory

    return {"memories": memory.load_active_memories(ctx.conn, ctx.project_id)}


# ------------------------------------------- graph tools (ticket #13, §20)
@register(
    "find_entity",
    "Find knowledge-graph entities in this project by name or alias.",
    {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)
def find_entity(ctx: ToolContext, args: dict) -> dict:
    rows = ctx.conn.execute(
        "SELECT DISTINCT e.id, e.type, e.canonical_name FROM entities e"
        " LEFT JOIN entity_aliases a ON a.entity_id = e.id"
        " WHERE e.project_id = ? AND"
        " (e.canonical_name LIKE ? OR a.alias LIKE ?) LIMIT 20",
        (ctx.project_id, f"%{args['query']}%", f"%{args['query']}%"),
    ).fetchall()
    return {"entities": [dict(r) for r in rows]}


@register(
    "query_graph",
    "Traverse the project knowledge graph from an entity: connected systems,"
    " dependencies, references (multi-hop, evidence-backed).",
    {
        "type": "object",
        "properties": {
            "entity": {"type": "string"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 3},
            "relation_type": {"type": "string"},
        },
        "required": ["entity"],
        "additionalProperties": False,
    },
)
def query_graph(ctx: ToolContext, args: dict) -> dict:
    depth = min(int(args.get("depth", 2)), 3)
    rel_filter = args.get("relation_type")

    start = ctx.conn.execute(
        "SELECT DISTINCT e.id FROM entities e"
        " LEFT JOIN entity_aliases a ON a.entity_id = e.id"
        " WHERE e.project_id = ? AND (e.canonical_name = ? OR a.alias = ?"
        " OR e.canonical_name LIKE ?) LIMIT 1",
        (ctx.project_id, args["entity"], args["entity"], f"%{args['entity']}%"),
    ).fetchone()
    if start is None:
        return {"error": f"entity not found in this project: {args['entity']}"}

    start_id = start["id"]
    names = {r["id"]: r["canonical_name"] for r in ctx.conn.execute(
        "SELECT id, canonical_name FROM entities WHERE project_id = ?",
        (ctx.project_id,)).fetchall()}
    nodes = {start_id}
    frontier = [start_id]
    edges = []
    for _ in range(depth):
        if not frontier:
            break
        marks = ",".join("?" for _ in frontier)
        sql = (
            "SELECT r.id, r.source_entity_id, r.relation_type,"
            " r.target_entity_id, r.confidence,"
            " s.canonical_name AS source, t.canonical_name AS target"
            " FROM relations r JOIN entities s ON s.id = r.source_entity_id"
            " JOIN entities t ON t.id = r.target_entity_id"
            f" WHERE r.project_id = ? AND (r.source_entity_id IN ({marks})"
            f" OR r.target_entity_id IN ({marks}))"
        )
        params = [ctx.project_id, *frontier, *frontier]
        if rel_filter:
            sql += " AND r.relation_type = ?"
            params.append(rel_filter)
        next_frontier = []
        for row in ctx.conn.execute(sql, params).fetchall():
            nodes.update((row["source_entity_id"], row["target_entity_id"]))
            edges.append({
                "relation_id": row["id"], "type": row["relation_type"],
                "source": row["source"], "target": row["target"],
                "confidence": row["confidence"],
                "evidence_chunks": [
                    e["chunk_id"] for e in ctx.conn.execute(
                        "SELECT chunk_id FROM relation_evidence WHERE relation_id = ?",
                        (row["id"],),
                    ).fetchall()
                ],
            })
            for nid in (row["source_entity_id"], row["target_entity_id"]):
                if nid not in nodes or nid not in next_frontier:
                    next_frontier.append(nid)
        frontier = [n for n in next_frontier if n not in frontier or n == start_id]
        frontier = list(dict.fromkeys(frontier))
    # deduplicate edges
    seen_edges = set()
    unique_edges = []
    for e in edges:
        key = e["relation_id"]
        if key not in seen_edges:
            seen_edges.add(key)
            unique_edges.append(e)
    return {
        "nodes": [{"id": nid, "name": names.get(nid)} for nid in sorted(nodes)],
        "edges": unique_edges,
    }


# ----------------------------------------------- artifact tools (ticket #8)
@register(
    "list_outputs",
    "List generated output artifacts in the project's outputs.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def list_outputs(ctx: ToolContext, args: dict) -> dict:
    from ..artifacts import manager

    return {"artifacts": manager.list_artifacts(ctx.conn, ctx.project_id)}


@register(
    "read_artifact",
    "Read a generated artifact by file name.",
    {
        "type": "object",
        "properties": {"file_name": {"type": "string"}},
        "required": ["file_name"],
        "additionalProperties": False,
    },
)
def read_artifact(ctx: ToolContext, args: dict) -> dict:
    from ..artifacts import manager

    rows = manager.list_artifacts(ctx.conn, ctx.project_id)
    match = next((a for a in rows if a["file_name"] == args["file_name"]), None)
    if match is None:
        return {"error": f"artifact not found: {args['file_name']}"}
    version = manager.read_version(ctx.conn, ctx.project_id, match["id"])
    return {"file_name": args["file_name"], "version": version["version"], "content": version["content"]}


@register(
    "write_artifact",
    "Create a new generated document in the project outputs (creates version 1"
    " if the file already exists as an artifact).",
    {
        "type": "object",
        "properties": {
            "file_name": {"type": "string"},
            "content": {"type": "string"},
            "source_context": {"type": "array"},
        },
        "required": ["file_name", "content"],
        "additionalProperties": False,
    },
)
def write_artifact(ctx: ToolContext, args: dict) -> dict:
    from ..artifacts import manager

    detail = manager.write_artifact(
        ctx.conn, ctx.settings, ctx.project_id,
        file_name=args["file_name"], content=args["content"],
        skill_id=ctx.skill_id, skill_version=ctx.skill_version,
        source_context=args.get("source_context") or [],
        created_by="skill",
    )
    return {"artifact_id": detail["id"], "file_name": detail["file_name"],
            "version": detail["current_version"]}


@register(
    "update_artifact",
    "Revise an existing generated document (preserved as a new version).",
    {
        "type": "object",
        "properties": {
            "file_name": {"type": "string"},
            "content": {"type": "string"},
            "source_context": {"type": "array"},
        },
        "required": ["file_name", "content"],
        "additionalProperties": False,
    },
)
def update_artifact(ctx: ToolContext, args: dict) -> dict:
    return write_artifact(ctx, args)


@register(
    "run_validator",
    "Run the declared validators against a generated artifact and return the"
    " results.",
    {
        "type": "object",
        "properties": {"file_name": {"type": "string"}, "skill_id": {"type": "string"}},
        "required": ["file_name"],
        "additionalProperties": False,
    },
)
def run_validator(ctx: ToolContext, args: dict) -> dict:
    from ..skills.validator import validate_artifact

    return validate_artifact(ctx.conn, ctx.settings, ctx.project_id,
                             args["file_name"], args.get("skill_id"))
