"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api, Evidence, Message, ProjectFile, RunEvent, Session,
} from "@/lib/api";

interface ChatMessage extends Message {
  pending?: boolean;
}

export default function ChatPage() {
  const params = useParams();
  const pid = String(params.projectId);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [runStatus, setRunStatus] = useState("");
  const [running, setRunning] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [selectedFiles, setSelectedFiles] = useState<string[]>([]);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [error, setError] = useState("");
  const logRef = useRef<HTMLDivElement>(null);

  const refreshSessions = useCallback(() => {
    api.listSessions(pid).then((r) => {
      setSessions(r.sessions);
      setSessionId((cur) => cur || r.sessions[0]?.id || null);
    }).catch((e) => setError(String(e)));
  }, [pid]);

  useEffect(() => {
    refreshSessions();
    api.listFiles(pid).then((r) => setFiles(r.files)).catch(() => {});
  }, [pid, refreshSessions]);

  useEffect(() => {
    if (!sessionId) return;
    api.listMessages(pid, sessionId).then((r) => setMessages(r.messages)).catch(() => {});
  }, [pid, sessionId]);

  useEffect(() => {
    logRef.current?.scrollTo(0, logRef.current.scrollHeight);
  }, [messages, runStatus]);

  function handleEvent(e: RunEvent) {
    switch (e.event) {
      case "run.started":
        setRunId(e.data.run_id === "run_pending" ? null : e.data.run_id);
        setRunStatus("Starting agent run…");
        break;
      case "context.search.started":
        setRunStatus("Searching project context…");
        break;
      case "context.search.completed":
        setRunStatus(e.data.sources?.length
          ? `Context found in: ${e.data.sources.join(", ")}`
          : "No matching context found");
        break;
      case "tool.started":
        setRunStatus(`Tool: ${e.data.tool}…`);
        break;
      case "tool.completed":
        setRunStatus(`Tool ${e.data.tool}: ${e.data.summary || "done"}`);
        break;
      case "skill.started":
        setRunStatus(`Running skill: ${e.data.skill}…`);
        break;
      case "skill.completed":
        setRunStatus(`Skill ${e.data.skill} completed`);
        break;
      case "assistant.delta":
        setRunStatus("");
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last?.pending) {
            return [...prev.slice(0, -1), { ...last, content: last.content + (e.data.delta || "") }];
          }
          return [...prev, { id: "pending", role: "assistant", content: e.data.delta || "", meta: {}, created_at: "", pending: true }];
        });
        break;
      case "run.waiting_user":
        setRunStatus("Waiting for your answer");
        break;
      case "run.completed": {
        const content = e.data.content || "";
        const refs: string[] = e.data.evidence_refs || [];
        setMessages((prev) => {
          const withoutPending = prev.filter((m) => !m.pending);
          if (content && !withoutPending.some((m) => m.content === content)) {
            return [...withoutPending, {
              id: `final-${Date.now()}`, role: "assistant", content,
              meta: { evidence_refs: refs }, created_at: "",
            }];
          }
          return withoutPending;
        });
        setRunStatus(e.data.status === "SUCCEEDED" ? "" : `Run ${e.data.status}`);
        setRunning(false);
        setRunId(null);
        break;
      }
      case "run.failed":
        setRunStatus("");
        setRunning(false);
        setRunId(null);
        setError(
          `${e.data.message}\n${(e.data.actions || []).map((a: string) => `• ${a}`).join("\n")}`);
        break;
      case "run.cancelled":
        setRunStatus("Run cancelled");
        setRunning(false);
        setRunId(null);
        break;
    }
  }

  async function send() {
    const message = input.trim();
    if (!message || running) return;
    setInput("");
    setError("");
    setMessages((prev) => [...prev, {
      id: `user-${Date.now()}`, role: "user", content: message,
      meta: { selected_files: selectedFiles }, created_at: "",
    }]);
    setRunning(true);
    setRunStatus("Thinking…");
    try {
      await api.chat(pid, {
        session_id: sessionId || undefined,
        message,
        selected_files: selectedFiles,
      }, handleEvent);
      refreshSessions();
      setMessages((prev) => prev.map((m) => ({ ...m, pending: false })));
    } catch (e) {
      setError(String(e));
      setRunning(false);
    }
  }

  async function cancel() {
    if (runId) await api.cancelRun(pid, runId).catch(() => {});
  }

  function toggleFile(name: string) {
    setSelectedFiles((prev) =>
      prev.includes(name) ? prev.filter((f) => f !== name) : [...prev, name]);
  }

  async function openEvidence(chunkId: string) {
    try {
      setEvidence(await api.evidence(pid, chunkId));
    } catch (e) {
      setError(String(e));
    }
  }

  function citationsOf(m: ChatMessage): string[] {
    const refs = (m.meta?.evidence_refs as string[]) || [];
    const inline = [...m.content.matchAll(/\[(chk_[A-Za-z0-9_]+)\]/g)].map((x) => x[1]);
    return [...new Set([...refs, ...inline])];
  }

  return (
    <>
      <div className="chatlog" ref={logRef}>
        <div style={{ maxWidth: 780, margin: "0 auto 16px" }} className="muted">
          Agent · Project context ready · {files.length} files
        </div>
        {messages.length === 0 && (
          <div style={{ maxWidth: 780, margin: "0 auto" }} className="muted">
            What would you like to do? Ask about the project, or type <code className="inline">/ba</code> to run the BA skill.
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            <div className="who">{m.role === "user" ? "You" : "Agent"}</div>
            <div className="bubble">{m.content}</div>
            {citationsOf(m).length > 0 && (
              <div className="citations">
                {citationsOf(m).map((c) => (
                  <button key={c} className="cite" onClick={() => openEvidence(c)}>
                    🔍 {c}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="chatinput">
        <div style={{ maxWidth: 780, margin: "0 auto 8px", display: "flex", flexWrap: "wrap", gap: 6 }}>
          {files.map((f) => (
            <button key={f.id}
                    className={`cite ${selectedFiles.includes(f.name) ? "active" : ""}`}
                    style={selectedFiles.includes(f.name) ? { borderColor: "var(--accent)", color: "var(--accent)" } : {}}
                    onClick={() => toggleFile(f.name)}>
              @{f.name}
            </button>
          ))}
        </div>
        <div className="row">
          <input
            type="text"
            placeholder="Ask about this project…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
            disabled={running}
          />
          {running ? (
            <button className="btn danger" onClick={cancel}>Cancel</button>
          ) : (
            <button className="btn" onClick={send}>Send</button>
          )}
        </div>
        <div className="runstatus">
          {runStatus}
          {sessions.length > 0 && (
            <span style={{ float: "right" }}>
              <select value={sessionId || ""} onChange={(e) => setSessionId(e.target.value)}>
                {sessions.map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}
              </select>{" "}
              <button className="btn small secondary" onClick={async () => {
                const s = await api.createSession(pid);
                setSessions((prev) => [s, ...prev]);
                setSessionId(s.id);
                setMessages([]);
              }}>New chat</button>{" "}
              <button className="btn small secondary" onClick={async () => {
                if (!sessionId) return;
                const title = window.prompt("Rename chat", sessions.find((s) => s.id === sessionId)?.title || "");
                if (title) { await api.renameSession(pid, sessionId, title); refreshSessions(); }
              }}>Rename</button>{" "}
              <button className="btn small danger" onClick={async () => {
                if (!sessionId) return;
                await api.deleteSession(pid, sessionId);
                setSessionId(null);
                setMessages([]);
                refreshSessions();
              }}>Delete</button>
            </span>
          )}
        </div>
        {error && <div className="error-text" style={{ maxWidth: 780, margin: "6px auto 0" }}>{error}</div>}
      </div>

      {evidence && (
        <div className="dialog-backdrop" onClick={() => setEvidence(null)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <h2>Evidence</h2>
            <p className="muted">
              {evidence.document}{evidence.page ? ` · page ${evidence.page}` : ""}
              {evidence.section_path?.length ? ` · ${evidence.section_path.join(" › ")}` : ""}
            </p>
            <div className="card" style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>{evidence.text}</div>
            <div style={{ marginTop: 12, textAlign: "right" }}>
              <button className="btn secondary" onClick={() => setEvidence(null)}>Close</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
