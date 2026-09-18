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
