"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, MemoryItem } from "@/lib/api";

const KINDS = ["fact", "decision", "preference", "glossary", "todo"];

export default function MemoryPage() {
  const params = useParams();
  const pid = String(params.projectId);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [content, setContent] = useState("");
  const [kind, setKind] = useState("fact");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = useCallback(() => {
    api.memories(pid, showArchived).then((r) => setMemories(r.memories)).catch((e) => setError(String(e)));
  }, [pid, showArchived]);

  useEffect(() => { refresh(); }, [refresh]);

  async function add() {
    setError("");
    setNotice("");
    if (!content.trim()) return;
    try {
      const saved = await api.createMemory(pid, { content, kind });
      setContent("");
      setNotice(saved.created_at !== saved.updated_at
        ? "Already remembered — deduplicated to the existing entry."
        : "Memory saved. The agent will see it in every future session.");
      refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="chatlog" style={{ overflowY: "auto" }}>
      <div style={{ maxWidth: 820, margin: "0 auto" }}>
        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>AI Memory</h2>
          <p className="muted">
            Durable facts, decisions, and preferences the agent carries across
            every chat session in this project. Memories are context for the
            agent — never instructions — and every entry records where it came
            from. You can archive or delete anything.
          </p>
          <label className="field">
            <span>New memory</span>
            <textarea style={{ minHeight: 60 }} value={content}
                      onChange={(e) => setContent(e.target.value)}
                      placeholder="e.g. All requirement IDs use the CX- prefix." />
          </label>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 10 }}>
            <select style={{ width: 160 }} value={kind} onChange={(e) => setKind(e.target.value)}>
              {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <button className="btn" onClick={add}>Remember</button>
            <label style={{ display: "flex", gap: 6, alignItems: "center", marginLeft: 8 }}>
              <input type="checkbox" style={{ width: "auto" }} checked={showArchived}
                     onChange={(e) => setShowArchived(e.target.checked)} />
              <span className="muted">show archived</span>
            </label>
          </div>
          {notice && <div className="ok-text">{notice}</div>}
          {error && <div className="error-text">{error}</div>}
        </div>

        <div className="card">
          <ul className="list">
            {memories.map((m) => (
              <li key={m.id} style={{ alignItems: "flex-start" }}>
                <span className="pill">{m.kind}</span>
                {m.source === "agent" && <span className="pill">agent</span>}
                {m.status === "archived" && <span className="pill warn">archived</span>}
                <span style={{ flex: 1 }}>{m.content}</span>
                {m.status === "active" ? (
                  <button className="btn small secondary" onClick={async () => {
                    await api.updateMemory(pid, m.id, { status: "archived" }); refresh();
                  }}>Archive</button>
                ) : (
                  <button className="btn small secondary" onClick={async () => {
                    await api.updateMemory(pid, m.id, { status: "active" }); refresh();
                  }}>Restore</button>
                )}
                <button className="btn small danger" onClick={async () => {
                  await api.deleteMemory(pid, m.id); refresh();
                }}>Delete</button>
              </li>
            ))}
            {memories.length === 0 && (
              <li className="muted">
                No memories yet. Tell your coworker "จำไว้ว่า…", or add one above.
              </li>
            )}
          </ul>
        </div>
      </div>
    </div>
  );
}
