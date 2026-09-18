"""Independent artifact verifier (loop-engineering: maker/checker split).

The agent that wrote an artifact never judges its own work: this verifier
is a separate, fresh LLM call with a default stance of REJECT — it looks
for reasons to fail the artifact against the project's actual evidence.
Its verdict is attached to the artifact version's validation record.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..llm.base import ChatMessage
from ..retrieval import text_search
from ..util import now_iso

VERIFIER_SYSTEM = (
    "You are a strict artifact verifier. Default verdict: REJECT. Actively "
    "look for reasons to reject: claims unsupported by the supplied evidence, "
    "invented facts, broken or missing chunk citations, contradictions, and "
    "missing required sections. Only PASS if everything important is "
    "evidence-backed.\n"
    "Respond with a single JSON object:\n"
    '{"verdict": "pass" | "fail", "issues": ["..."], "notes": "..."}'
)


def _evidence_for_artifact(conn: sqlite3.Connection, project_id: str, content: str,
                           source_context: list) -> str:
    """Cited chunks (verbatim) plus a light retrieval pass for grounding."""
    import re

    cited = sorted(set(re.findall(r"\bchk_[A-Za-z0-9_]+\b", content)))
    for entry in source_context:
        cid = entry.get("chunk_id") if isinstance(entry, dict) else entry
        if cid and cid not in cited:
            cited.append(cid)

    blocks = []
    for cid in cited[:8]:
        row = conn.execute(
            "SELECT c.text, c.page, d.name AS document FROM chunks c"
            " JOIN documents d ON d.id = c.document_id"
            " WHERE c.id = ? AND c.project_id = ?",
            (cid, project_id),
        ).fetchone()
        if row:
            blocks.append(f"[{cid} | {row['document']} p{row['page']}]\n{row['text'][:800]}")

    if not blocks:  # no explicit citations — ground with retrieval instead
        query = " ".join(re.findall(r"[A-Za-z\u0e00-\u0e7f]{4,}", content))[:200]
        for r in text_search.text_search(conn, project_id, query, limit=5):
            blocks.append(f"[{r['chunk_id']} | {r['document_name']}]\n{r['text'][:800]}")

    return "\n\n".join(blocks) or "(no project evidence found)"


def verify_artifact(
    conn: sqlite3.Connection,
    client,  # LLMClient — a fresh call, no shared conversation
    project_id: str,
    file_name: str,
    content: str,
    source_context: list | None = None,
) -> dict:
    """Returns {verdict, issues, notes, checked_at}. Never raises."""
    evidence = _evidence_for_artifact(conn, project_id, content, source_context or [])
    started = now_iso()
    try:
        resp = client.complete([
            ChatMessage(role="system", content=VERIFIER_SYSTEM),
            ChatMessage(role="user",
                        content="Artifact to verify:\n\n"
                                f"{content[:12000]}\n\n"
                                f"Project evidence:\n\n{evidence}"),
        ])
        raw = (resp.content or "").strip()
        parsed = _parse_verdict(raw)
        if parsed is None:
            return {"verdict": "unverified", "issues": [],
                    "notes": f"verifier returned unparseable output: {raw[:200]}",
                    "checked_at": started}
        parsed["checked_at"] = started
        return parsed
    except Exception as e:  # noqa: BLE001 — verifier failure must not fail the run
        return {"verdict": "unverified", "issues": [],
                "notes": f"verifier unavailable: {e}", "checked_at": started}


def _parse_verdict(raw: str) -> dict | None:
    import re

    text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw.strip())
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict) or parsed.get("verdict") not in ("pass", "fail"):
        return None
    parsed.setdefault("issues", [])
    parsed.setdefault("notes", "")
    return parsed


def attach_verifier_result(
    conn: sqlite3.Connection, project_id: str, artifact_id: str, version: int,
    result: dict[str, Any],
) -> None:
    """Fold the verifier verdict into the version's validation record and the
    artifact's overall status."""
    row = conn.execute(
        "SELECT validation FROM artifact_versions WHERE artifact_id = ? AND version = ?",
        (artifact_id, version),
    ).fetchone()
    if row is None:
        return
    validation = json.loads(row["validation"] or "{}")
    validation["verifier"] = result
    deterministic = validation.get("validation_status")
    if result["verdict"] == "fail" or deterministic == "failed":
        overall = "failed"
    elif result["verdict"] == "pass":
        overall = "passed" if deterministic != "failed" else "failed"
    else:
        # verifier unavailable/unparseable: keep the deterministic result
        overall = deterministic if deterministic in ("passed", "failed") else "unverified"
    validation["validation_status"] = overall
    conn.execute(
        "UPDATE artifact_versions SET validation = ? WHERE artifact_id = ? AND version = ?",
        (json.dumps(validation, ensure_ascii=False), artifact_id, version),
    )
    conn.execute(
        "UPDATE artifacts SET validation_status = ? WHERE id = ?",
        (overall, artifact_id),
    )
    conn.commit()
