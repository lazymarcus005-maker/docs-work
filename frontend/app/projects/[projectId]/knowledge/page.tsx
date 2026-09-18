"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, Entity, Relation, ReviewItem } from "@/lib/api";

export default function KnowledgePage() {
  const params = useParams();
  const pid = String(params.projectId);
  const [entities, setEntities] = useState<Entity[]>([]);
  const [relations, setRelations] = useState<Relation[]>([]);
  const [review, setReview] = useState<ReviewItem[]>([]);
  const [health, setHealth] = useState<Record<string, number>>({});
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Entity | null>(null);
  const [detail, setDetail] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(() => {
    api.entities(pid, query || undefined).then((r) => setEntities(r.entities)).catch((e) => setError(String(e)));
    api.relations(pid).then((r) => setRelations(r.relations)).catch(() => {});
    api.review(pid).then((r) => setReview(r.items)).catch(() => {});
    api.health(pid).then(setHealth).catch(() => {});
  }, [pid, query]);

  useEffect(() => { refresh(); }, [refresh]);

  async function openEntity(e: Entity) {
    setSelected(e);
    setDetail(await api.entities(pid).then(() =>
      fetch(`http://localhost:8000/api/projects/${pid}/entities/${e.id}`).then((r) => r.json())));
  }

  return (
    <div className="chatlog" style={{ overflowY: "auto" }}>
      <div style={{ maxWidth: 960, margin: "0 auto" }}>
        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>Context Health</h2>
          <table className="plain">
            <tbody>
              <tr><td>Documents ready</td><td>{health.documents_ready ?? 0} / {health.documents_total ?? 0}</td></tr>
              <tr><td>Parser failures</td><td>{health.parser_failures ?? 0}</td></tr>
              <tr><td>Open review items</td><td>{health.open_review_items ?? 0}</td></tr>
              <tr><td>Conflicts</td><td>{health.conflicts ?? 0}</td></tr>
              <tr><td>Entities / Relations</td><td>{health.entities ?? 0} / {health.relations ?? 0}</td></tr>
            </tbody>
          </table>
        </div>

        {review.length > 0 && (
          <div className="card">
            <h2 style={{ marginTop: 0, fontSize: 16 }}>Review Queue</h2>
            <ul className="list">
              {review.map((item) => (
                <li key={item.id}>
                  <span className="pill warn">{item.kind}</span>
                  <span style={{ flex: 1 }}>
                    {item.kind === "duplicate_entity"
                      ? `Merge ${((item.payload.names as string[]) || []).join(" ↔ ")}?`
                      : item.kind === "conflict"
                        ? `Conflict on ${item.payload.subject}: ${JSON.stringify(item.payload.values)}`
                        : JSON.stringify(item.payload)}
                  </span>
                  {item.kind === "duplicate_entity" && (
                    <>
                      <button className="btn small" onClick={async () => {
                        await api.resolveReview(pid, item.id, "merge"); refresh();
                      }}>Merge</button>
                      <button className="btn small secondary" onClick={async () => {
                        await api.resolveReview(pid, item.id, "keep_separate"); refresh();
                      }}>Keep separate</button>
                    </>
                  )}
                  {item.kind !== "duplicate_entity" && (
                    <button className="btn small secondary" onClick={async () => {
                      await api.resolveReview(pid, item.id, "dismiss"); refresh();
                    }}>Dismiss</button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="grid2">
          <div className="card">
            <h2 style={{ marginTop: 0, fontSize: 16 }}>Entities</h2>
            <input type="text" placeholder="Search entities…" value={query}
                   onChange={(e) => setQuery(e.target.value)} />
            <ul className="list" style={{ marginTop: 8 }}>
              {entities.map((e) => (
                <li key={e.id}>
                  <a href="#" onClick={(ev) => { ev.preventDefault(); openEntity(e); }}>
                    {e.canonical_name}
                  </a>
                  <span className="pill">{e.type}</span>
                  <span style={{ flex: 1 }} />
                  <span className="muted">{e.degree} links</span>
                </li>
              ))}
              {entities.length === 0 && <li className="muted">No entities extracted yet.</li>}
            </ul>
          </div>

          <div className="card">
            <h2 style={{ marginTop: 0, fontSize: 16 }}>Relations</h2>
            <ul className="list">
              {relations.map((r) => (
                <li key={r.id}>
                  <span><strong>{r.source}</strong> — {r.relation_type} → <strong>{r.target}</strong></span>
                  <span style={{ flex: 1 }} />
                  <span className="muted">{(r.confidence * 100).toFixed(0)}%</span>
                  <span className="pill">{r.evidence.length} evid.</span>
                </li>
              ))}
              {relations.length === 0 && <li className="muted">No relations extracted yet.</li>}
            </ul>
          </div>
        </div>

        {detail && selected && (
          <div className="card">
            <h2 style={{ marginTop: 0, fontSize: 16 }}>
              {selected.canonical_name} <span className="pill">{selected.type}</span>
            </h2>
            {detail.aliases?.length > 1 && (
              <div className="muted">Aliases: {detail.aliases.join(", ")}</div>
            )}
            {detail.relations?.map((r: any) => (
              <div key={r.id} className="muted" style={{ marginTop: 6 }}>
                {r.source} — {r.relation_type} → {r.target}
                {r.evidence?.length > 0 && (
                  <div style={{ fontSize: 12 }}>
                    evidence: {r.evidence[0].chunk_id} — {String(r.evidence[0].text).slice(0, 120)}…
                  </div>
                )}
              </div>
            ))}
            <div style={{ marginTop: 10 }}>
              <button className="btn secondary small" onClick={() => { setDetail(null); setSelected(null); }}>
                Close
              </button>
            </div>
          </div>
        )}
        {error && <div className="error-text">{error}</div>}
      </div>
    </div>
  );
}
