"use client";

import Link from "next/link";
import { usePathname, useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, Project, ProjectFile, Artifact, Session, Skill } from "@/lib/api";

export default function WorkspaceLayout({ children }: { children: React.ReactNode }) {
  const params = useParams();
  const pathname = usePathname();
  const router = useRouter();
  const pid = String(params.projectId);
  const [project, setProject] = useState<Project | null>(null);
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);

  useEffect(() => {
    api.getProject(pid).then(setProject).catch(() => {});
    api.listFiles(pid).then((r) => setFiles(r.files)).catch(() => {});
    api.listArtifacts(pid).then((r) => setArtifacts(r.artifacts)).catch(() => {});
    api.projectSkills(pid).then((r) => setSkills(r.skills)).catch(() => {});
    api.listSessions(pid).then((r) => setSessions(r.sessions)).catch(() => {});
  }, [pid, pathname]);

  const is = (suffix: string) => pathname === `/projects/${pid}${suffix}`;

  function openSession(sid: string | null) {
    sessionStorage.setItem(`cowork-session-${pid}`, sid || "");
    router.push(`/projects/${pid}`);
  }

  return (
    <>
      <header className="topbar">
        <Link href="/">← Projects</Link>
        <span className="brand">{project?.name || "…"}</span>
        <span style={{ flex: 1 }} />
        <Link href={`/projects/${pid}/files`} className="muted">Processing</Link>
        <Link href="/settings" className="muted">Settings</Link>
      </header>
      <div className="workspace">
        <aside className="sidebar">
          <h3>Workspace</h3>
          <Link className={`item ${is("") ? "active-item" : ""}`} href={`/projects/${pid}`}>
            💬 Chat
          </Link>
          <Link className={`item ${is("/files") ? "active-item" : ""}`} href={`/projects/${pid}/files`}>
            ⚙ Processing
          </Link>
          <Link className={`item ${is("/memory") ? "active-item" : ""}`} href={`/projects/${pid}/memory`}>
            🧠 Memory
          </Link>

          <h3>Conversations</h3>
          <button className="item" style={{ all: "unset", cursor: "pointer", display: "block" }}
                  onClick={() => openSession("")}>
            ＋ New chat
          </button>
          {sessions.map((s) => (
            <button key={s.id} className="item" title={s.title}
                    style={{ all: "unset", cursor: "pointer", display: "block", textAlign: "left" }}
                    onClick={() => openSession(s.id)}>
              💬 {s.title}
            </button>
          ))}
          {sessions.length === 0 && <div className="muted" style={{ padding: "0 8px" }}>No conversations yet</div>}

          <h3>Context</h3>
          {files.map((f) => (
            <Link key={f.id} className="item" href={`/projects/${pid}/files`} title={`${f.name} (${f.status})`}>
              {f.status === "READY" ? "✓" : f.status === "FAILED" ? "⚠" : "→"} {f.name}
            </Link>
          ))}
          {files.length === 0 && <div className="muted" style={{ padding: "0 8px" }}>No files yet</div>}

          <h3>Outputs</h3>
          {artifacts.map((a) => (
            <Link key={a.id} className="item" href={`/projects/${pid}/outputs`}>
              📄 {a.file_name} <span className="muted">v{a.current_version}</span>
            </Link>
          ))}
          {artifacts.length === 0 && <div className="muted" style={{ padding: "0 8px" }}>No outputs yet</div>}

          <h3>Skills</h3>
          {skills.filter((s) => s.enabled).map((s) => (
            <Link key={s.id} className="item" href={`/projects/${pid}/skills`}>
              ⚡ {s.name}
            </Link>
          ))}
        </aside>
        <main className="main">{children}</main>
      </div>
    </>
  );
}
