"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, Artifact, ArtifactDetail, Evidence } from "@/lib/api";

export default function OutputsPage() {
  const params = useParams();
  const pid = String(params.projectId);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selected, setSelected] = useState<ArtifactDetail | null>(null);
  const [content, setContent] = useState("");
  const [version, setVersion] = useState<number | null>(null);
  const [evidence, setEvidence] = useState<{ citations: Evidence[]; unresolved: { chunk_id: string }[] } | null>(null);
  const [error, setError] = useState("");
  const [dirty, setDirty] = useState(false);

  const refresh = useCallback(() => {
    api.listArtifacts(pid).then((r) => setArtifacts(r.artifacts)).catch((e) => setError(String(e)));
  }, [pid]);

  useEffect(() => { refresh(); }, [refresh]);

  async function open(a: Artifact) {
    setSelected(await api.getArtifact(pid, a.id));
    setVersion(null);
    setEvidence(null);
    setDirty(false);
    const c = await api.artifactContent(pid, a.id);
    setContent(c.content);
  }

  async function save() {
    if (!selected) return;
    try {
      await api.editArtifact(pid, selected.id, content);
      await open({ ...selected });
      refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="chatlog" style={{ overflowY: "auto" }}>
      <div style={{ maxWidth: 920, margin: "0 auto" }} className="grid2">
        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>Generated Outputs</h2>
          <ul className="list">
            {artifacts.map((a) => (
              <li key={a.id}>
                <a href="#" onClick={(e) => { e.preventDefault(); open(a); }}>
                  <strong>{a.file_name}</strong>
                </a>
                <span className="pill">v{a.current_version}</span>
                {a.validation_status === "passed" && <span className="pill ok">validated</span>}
                {a.validation_status === "failed" && <span className="pill warn">validation failed</span>}
                {a.validation_status === "proposed" && <span className="pill warn">proposed — needs approval</span>}
                {a.skill_id && <span className="muted">by {a.skill_id}</span>}
              </li>
            ))}
            {artifacts.length === 0 && <li className="muted">No outputs yet — run a skill from chat.</li>}
          </ul>
        </div>

        <div className="card">
          {selected ? (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <h2 style={{ margin: 0, fontSize: 16, flex: 1 }}>{selected.file_name}</h2>
                <select value={version ?? selected.current_version}
                        onChange={async (e) => {
                          const v = Number(e.target.value);
                          setVersion(v);
                          setContent((await api.artifactContent(pid, selected.id, v)).content);
                        }}>
                  {selected.versions.map((v) => (
                    <option key={v.version} value={v.version}>
                      v{v.version} ({v.created_by})
                    </option>
                  ))}
                </select>
              </div>
              <textarea style={{ minHeight: 320, marginTop: 10, fontFamily: "ui-monospace, monospace", fontSize: 12 }}
                        value={content}
                        onChange={(e) => { setContent(e.target.value); setDirty(true); }} />
              <div style={{ marginTop: 10, display: "flex", gap: 8 }}>
                {selected.validation_status === "proposed" ? (
                  <button className="btn" onClick={async () => {
                    const d = await api.approveArtifact(pid, selected.id);
                    setSelected(d);
                    const c = await api.artifactContent(pid, selected.id);
                    setContent(c.content);
                    refresh();
                  }}>Approve (L1)</button>
                ) : (
                  <button className="btn" onClick={save} disabled={!dirty}>Save as new version</button>
                )}
                <button className="btn secondary" onClick={async () => {
                  setEvidence(await api.artifactEvidence(pid, selected.id));
                }}>View Evidence</button>
                <a className="btn secondary" href={`http://localhost:8000/api/projects/${pid}/artifacts/${selected.id}/download`}>Download</a>
              </div>
              {error && <div className="error-text">{error}</div>}
              {evidence && (
                <div style={{ marginTop: 12 }}>
                  <h3 style={{ fontSize: 13 }}>Evidence ({evidence.citations.length})</h3>
                  {evidence.citations.map((c) => (
                    <div key={c.chunk_id} className="card" style={{ padding: 8 }}>
                      <span className="pill">{c.chunk_id}</span>{" "}
                      <span className="muted">{c.document}{c.page ? ` p${c.page}` : ""}</span>
                      <div className="muted" style={{ marginTop: 4, whiteSpace: "pre-wrap" }}>
                        {c.text.slice(0, 240)}…
                      </div>
                    </div>
                  ))}
                  {evidence.unresolved.length > 0 && (
                    <div className="error-text">Unresolved citations: {evidence.unresolved.map((u) => u.chunk_id).join(", ")}</div>
                  )}
                </div>
              )}
            </>
          ) : (
            <div className="muted">Select an artifact to view or edit it.</div>
          )}
        </div>
      </div>
    </div>
  );
}
