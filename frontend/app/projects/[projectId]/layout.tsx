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
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  useEffect(() => {
    const preference = localStorage.getItem("cowork-sidebar-collapsed");
    setSidebarCollapsed(
      preference === null
        ? window.matchMedia("(max-width: 560px)").matches
        : preference === "true",
    );
  }, []);

  useEffect(() => {
    api.getProject(pid).then(setProject).catch(() => {});
    api.listFiles(pid).then((r) => setFiles(r.files)).catch(() => {});
    api.listArtifacts(pid).then((r) => setArtifacts(r.artifacts)).catch(() => {});
    api.projectSkills(pid).then((r) => setSkills(r.skills)).catch(() => {});
    api.listSessions(pid).then((r) => setSessions(r.sessions)).catch(() => {});
  }, [pid, pathname]);

  useEffect(() => {
    const refreshSessions = () => api.listSessions(pid).then((r) => setSessions(r.sessions)).catch(() => {});
    window.addEventListener("cowork:sessions-refresh", refreshSessions);
    return () => window.removeEventListener("cowork:sessions-refresh", refreshSessions);
  }, [pid]);

  const is = (suffix: string) => pathname === `/projects/${pid}${suffix}`;

  function openSession(sid: string | null) {
    sessionStorage.setItem(`cowork-session-${pid}`, sid || "");
    window.dispatchEvent(new CustomEvent("cowork:session-change", { detail: sid }));
    router.push(`/projects/${pid}`);
  }

  function toggleSidebar() {
    const next = !sidebarCollapsed;
    setSidebarCollapsed(next);
    localStorage.setItem("cowork-sidebar-collapsed", String(next));
  }

  async function renameSession(session: Session) {
    const title = window.prompt("Rename work session", session.title);
    if (title?.trim()) {
      await api.renameSession(pid, session.id, title.trim());
      api.listSessions(pid).then((r) => setSessions(r.sessions));
    }
  }

  async function deleteSession(session: Session) {
    if (!window.confirm(`Delete “${session.title}” and its conversation history?`)) return;
    await api.deleteSession(pid, session.id);
    if (sessionStorage.getItem(`cowork-session-${pid}`) === session.id) openSession(null);
    api.listSessions(pid).then((r) => setSessions(r.sessions));
  }

  return (
    <>
      <header className="topbar">
        <Link href="/">← Projects</Link>
        <button
          type="button"
          className="sidebar-toggle"
          aria-label={sidebarCollapsed ? "Expand navigation" : "Collapse navigation"}
          aria-controls="project-sidebar"
          aria-expanded={!sidebarCollapsed}
          title={sidebarCollapsed ? "Expand navigation" : "Collapse navigation"}
          onClick={toggleSidebar}
        >
          <span aria-hidden="true">{sidebarCollapsed ? "›" : "‹"}</span>
        </button>
        <span className="brand">{project?.name || "…"}</span>
        <select
          aria-label="Autonomy level"
          style={{ width: "auto", fontSize: 12 }}
          value={project?.autonomy_level ?? 2}
          onChange={async (e) => {
            const level = Number(e.target.value);
            setProject(await api.updateProject(pid, { autonomy_level: level }));
          }}>
          <option value={1}>L1 · propose (needs approval)</option>
          <option value={2}>L2 · assisted</option>
          <option value={3}>L3 · autonomous</option>
        </select>
        <span style={{ flex: 1 }} />
        <div className="topbar-meta"><span className="live-dot" /> Local workspace</div>
        <Link href="/settings" className="topbar-link">Settings</Link>
      </header>
      <div className={`workspace ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
        <aside className="sidebar" id="project-sidebar">
          <div className="sidebar-project">
            <span className="project-mark">{(project?.name || "W").slice(0, 1).toUpperCase()}</span>
            <div className="project-details"><strong>{project?.name || "Project"}</strong><small>Project workspace</small></div>
          </div>
          <h3>Workspace</h3>
          <Link className={`item ${is("") ? "active-item" : ""}`} href={`/projects/${pid}`} title="Cowork" aria-label="Cowork">
            <span className="nav-icon">✳</span><span className="nav-label">Cowork</span>
          </Link>
          <Link className={`item ${is("/tasks") ? "active-item" : ""}`} href={`/projects/${pid}/tasks`} title="Tasks" aria-label="Tasks">
            <span className="nav-icon">☷</span><span className="nav-label">Tasks</span>
          </Link>
          <Link className={`item ${is("/files") ? "active-item" : ""}`} href={`/projects/${pid}/files`} title="Files" aria-label="Files">
            <span className="nav-icon">▤</span><span className="nav-label">Files</span>
          </Link>
          <Link className={`item ${is("/knowledge") ? "active-item" : ""}`} href={`/projects/${pid}/knowledge`} title="Knowledge" aria-label="Knowledge">
            <span className="nav-icon">◈</span><span className="nav-label">Knowledge</span>
          </Link>

          <h3>Recent</h3>
          <button className="item" title="New Task" aria-label="New Task"
                  onClick={() => openSession("")}>
            <span className="nav-icon">＋</span><span className="nav-label">New Task</span>
          </button>
          {sessions.map((s) => (
            <div key={s.id} className="session-item-row">
              <button className="item" title={s.title} aria-label={s.title}
                      onClick={() => openSession(s.id)}>
                <span className="nav-icon">◷</span><span className="nav-label">{s.title}</span>
              </button>
              <details className="session-actions">
                <summary aria-label={`Actions for ${s.title}`}>···</summary>
                <div className="session-actions-menu">
                  <button onClick={() => renameSession(s)}>Rename</button>
                  <button onClick={() => deleteSession(s)}>Delete</button>
                </div>
              </details>
            </div>
          ))}
          {sessions.length === 0 && <div className="sidebar-empty muted">No conversations yet</div>}

          <h3>Project files</h3>
          {files.map((f) => (
            <Link key={f.id} className="item" href={`/projects/${pid}/files`} title={`${f.name} (${f.status})`} aria-label={`${f.name} (${f.status})`}>
              <span className="nav-icon">{f.status === "READY" ? "✓" : f.status === "FAILED" ? "⚠" : "→"}</span>
              <span className="nav-label">{f.name}</span>
            </Link>
          ))}
          {files.length === 0 && <div className="sidebar-empty muted">No files yet</div>}

          <h3>Artifacts</h3>
          {artifacts.map((a) => (
            <Link key={a.id} className="item" href={`/projects/${pid}/outputs`} title={`${a.file_name} v${a.current_version}`} aria-label={`${a.file_name} v${a.current_version}`}>
              <span className="nav-icon">▤</span>
              <span className="nav-label">{a.file_name} <span className="muted">v{a.current_version}</span></span>
            </Link>
          ))}
          {artifacts.length === 0 && <div className="sidebar-empty muted">No artifacts yet</div>}

          <h3>Skills</h3>
          {skills.filter((s) => s.enabled).map((s) => (
            <Link key={s.id} className="item" href={`/projects/${pid}/skills`} title={s.name} aria-label={s.name}>
              <span className="nav-icon">⚡</span><span className="nav-label">{s.name}</span>
            </Link>
          ))}
        </aside>
        <main className="main">{children}</main>
      </div>
    </>
  );
}
