"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";

export default function CreateProjectPage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [instruction, setInstruction] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setError("");
    if (!name.trim()) {
      setError("Project name is required.");
      return;
    }
    setBusy(true);
    try {
      const project = await api.createProject({ name, description, instruction });
      if (files.length > 0) {
        await api.uploadFiles(project.id, files);
      }
      router.push(`/projects/${project.id}`);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  return (
    <>
      <header className="topbar">
        <Link href="/">← Projects</Link>
        <span className="brand">Create Project</span>
      </header>
      <main className="container" style={{ maxWidth: 640 }}>
        <div className="card">
          <label className="field">
            <span>Project Name *</span>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)}
                   placeholder="CXGateway Migration" />
          </label>
          <label className="field">
            <span>Description</span>
            <input type="text" value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <label className="field">
            <span>Project Instruction</span>
            <textarea value={instruction} onChange={(e) => setInstruction(e.target.value)}
                      placeholder={"You are a Business Analyst.\nUse project evidence only.\nNever invent missing requirements.\nCite source evidence where possible."} />
          </label>
          <label className="field">
            <span>Project Files (PDF, DOCX, PPTX, XLSX, CSV, MD, TXT, images)</span>
            <input type="file" multiple onChange={(e) => setFiles(Array.from(e.target.files || []))} />
          </label>
          {files.length > 0 && (
            <div className="muted" style={{ marginBottom: 12 }}>
              {files.length} file(s): {files.map((f) => f.name).join(", ")}
            </div>
          )}
          {error && <div className="error-text" style={{ marginBottom: 12 }}>{error}</div>}
          <button className="btn" onClick={submit} disabled={busy}>
            {busy ? "Creating…" : "Create Project"}
          </button>
        </div>
      </main>
    </>
  );
}
