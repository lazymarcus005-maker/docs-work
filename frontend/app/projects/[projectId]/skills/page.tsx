"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, ProjectFile, RunEvent, Skill } from "@/lib/api";

export default function SkillsPage() {
  const params = useParams();
  const pid = String(params.projectId);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [instruction, setInstruction] = useState("");
  const [runningSkill, setRunningSkill] = useState<string | null>(null);
  const [progress, setProgress] = useState("");
  const [result, setResult] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(() => {
    api.projectSkills(pid).then((r) => setSkills(r.skills)).catch((e) => setError(String(e)));
    api.listFiles(pid).then((r) => setFiles(r.files)).catch(() => {});
  }, [pid]);

  useEffect(() => { refresh(); }, [refresh]);

  function handleEvent(e: RunEvent) {
    if (e.event === "skill.started") {
      setRunningSkill(e.data.skill);
      setProgress(`Running ${e.data.skill}…`);
    } else if (e.event === "skill.completed") {
      setProgress(`${e.data.skill} completed`);
      setResult(`Output: ${(e.data.artifacts || []).join(", ") || "(no files)"}`);
    } else if (e.event === "tool.started") {
      setProgress(`Tool: ${e.data.tool}…`);
    } else if (e.event === "run.completed") {
      setRunningSkill(null);
      setProgress("");
      if (!result) setResult(e.data.status === "SUCCEEDED" ? "Done." : `Run ${e.data.status}`);
    } else if (e.event === "run.failed") {
      setRunningSkill(null);
      setProgress("");
      setError(`${e.data.message}\n${(e.data.actions || []).join("\n")}`);
    }
  }

  async function run(skill: Skill) {
    setError("");
    setResult("");
    try {
      await api.runSkill(pid, skill.id, {
        instruction: instruction || `Run ${skill.name}`,
        selected_files: selected,
      }, handleEvent);
    } catch (e) {
      setError(String(e));
      setRunningSkill(null);
    }
  }

  return (
    <div className="chatlog" style={{ overflowY: "auto" }}>
      <div style={{ maxWidth: 760, margin: "0 auto" }}>
        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>Skills</h2>
          <ul className="list">
            {skills.map((s) => (
              <li key={s.id}>
                <strong>{s.name}</strong>
                <span className="pill">v{s.version}</span>
                {s.builtin && <span className="pill">built-in</span>}
                <span className="muted" style={{ flex: 1 }}>{s.description}</span>
                <button className="btn small secondary"
                        onClick={async () => { await api.toggleSkill(pid, s.id, !s.enabled); refresh(); }}>
                  {s.enabled ? "Disable" : "Enable"}
                </button>
                <button className="btn small" disabled={!s.enabled || runningSkill !== null}
                        onClick={() => run(s)}>
                  {runningSkill === s.id ? "Running…" : "Run"}
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="card">
          <label className="field">
            <span>Instruction for the skill</span>
            <textarea value={instruction} onChange={(e) => setInstruction(e.target.value)}
                      placeholder="Generate requirements from the selected context…" />
          </label>
          <label className="field">
            <span>Selected files (used as prioritized context)</span>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {files.map((f) => (
                <button key={f.id} className={`cite ${selected.includes(f.name) ? "active" : ""}`}
                        style={selected.includes(f.name) ? { borderColor: "var(--accent)", color: "var(--accent)" } : {}}
                        onClick={() => setSelected((prev) =>
                          prev.includes(f.name) ? prev.filter((x) => x !== f.name) : [...prev, f.name])}>
                  @{f.name}
                </button>
              ))}
              {files.length === 0 && <span className="muted">No files uploaded yet.</span>}
            </div>
          </label>
          {progress && <div className="muted">{progress}</div>}
          {result && <div className="ok-text">{result}</div>}
          {error && <div className="error-text">{error}</div>}
        </div>
      </div>
    </div>
  );
}
