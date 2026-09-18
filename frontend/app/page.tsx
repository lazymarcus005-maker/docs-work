"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, Project } from "@/lib/api";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    api.listProjects().then((r) => setProjects(r.projects)).catch((e) => setError(String(e)));
  }, []);

  return (
    <>
      <header className="topbar">
        <span className="brand">Local Cowork Knowledge Workspace</span>
        <span className="spacer" />
        <Link href="/settings">Settings</Link>
      </header>
      <main className="container">
        <div className="card" style={{ display: "flex", alignItems: "center" }}>
          <div>
            <h1 style={{ margin: 0, fontSize: 18 }}>Projects</h1>
            <div className="muted">Upload documents, add instructions, and work with your project agent.</div>
          </div>
          <span style={{ flex: 1 }} />
          <Link href="/projects/new" className="btn">Create Project</Link>
        </div>

        {error && <div className="error-text">{error}</div>}

        <div className="card">
          {projects.length === 0 ? (
            <div className="muted">
              No projects yet. <Link href="/projects/new">Create your first project</Link> to get started.
            </div>
          ) : (
            <ul className="list">
              {projects.map((p) => (
                <li key={p.id}>
                  <Link href={`/projects/${p.id}`}><strong>{p.name}</strong></Link>
                  <span className="muted">{p.description}</span>
                  <span style={{ flex: 1 }} />
                  <span className="pill">{p.file_count ?? 0} files</span>
                  <span className="pill ok">{p.ready_count ?? 0} ready</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </main>
    </>
  );
}
