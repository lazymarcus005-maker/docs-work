"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, Check, FileText, Plus, Sparkles, Square, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  api, Evidence, Message, ProjectFile, RunEvent, Session, Skill,
  Artifact, WorkPlan, WorkTask,
} from "@/lib/api";

interface ChatMessage extends Message {
  pending?: boolean;
}

export default function ChatPage() {
  const params = useParams();
  const pid = String(params.projectId);
  const sessionKey = `cowork-session-${pid}`;
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [runStatus, setRunStatus] = useState("");
  const [running, setRunning] = useState(false);
  const runningRef = useRef(false);
  const activeRunSessionIdRef = useRef<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [plans, setPlans] = useState<WorkPlan[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [queuedInstructions, setQueuedInstructions] = useState<string[]>([]);
  const queuedRef = useRef<string[]>([]);
  const [activities, setActivities] = useState<{ id: string; text: string }[]>([]);
  const [selectedFiles, setSelectedFiles] = useState<string[]>([]);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [error, setError] = useState("");
  const logRef = useRef<HTMLDivElement>(null);

  const refreshSessions = useCallback(() => {
    api.listSessions(pid).then((r) => {
      setSessions(r.sessions);
      // restore the remembered session; "" means an explicit fresh chat
      setSessionId((cur) => {
        if (cur) return cur;
        const stored = sessionStorage.getItem(sessionKey);
        if (stored === "") return null;
        if (stored && r.sessions.some((s) => s.id === stored)) return stored;
        return r.sessions[0]?.id ?? null;
      });
    }).catch((e) => setError(String(e)));
  }, [pid, sessionKey]);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    const onSessionChange = (event: Event) => {
      const sid = (event as CustomEvent<string | null>).detail;
      sessionIdRef.current = sid;
      setSessionId(sid);
      if (!sid) {
        setMessages([]);
        setPlans([]);
        setActivities([]);
      }
    };
    window.addEventListener("cowork:session-change", onSessionChange);
    return () => window.removeEventListener("cowork:session-change", onSessionChange);
  }, []);

  useEffect(() => {
    refreshSessions();
    api.listFiles(pid).then((r) => setFiles(r.files)).catch(() => {});
    api.projectSkills(pid).then((r) => setSkills(r.skills.filter((s) => s.enabled))).catch(() => {});
    api.listArtifacts(pid).then((r) => setArtifacts(r.artifacts)).catch(() => {});
  }, [pid, refreshSessions]);

  // reload history when the selected session changes — but never mid-run,
  // otherwise a reload would wipe the streaming answer off the screen
  useEffect(() => {
    if (!sessionId) return;
    sessionStorage.setItem(sessionKey, sessionId);
    if (runningRef.current) return;
    api.listMessages(pid, sessionId).then((r) => setMessages(r.messages)).catch(() => {});
    api.workPlans(pid, sessionId).then((r) => setPlans(r.plans)).catch(() => {});
    setActivities([]);
  }, [pid, sessionId, sessionKey]);

  useEffect(() => {
    logRef.current?.scrollTo(0, logRef.current.scrollHeight);
  }, [messages, runStatus, plans, activities, queuedInstructions]);

  function handleEvent(e: RunEvent) {
    const activity = (text: string) => setActivities((prev) => [
      ...prev.slice(-5), { id: `${Date.now()}-${Math.random()}`, text },
    ]);
    switch (e.event) {
      case "plan.created":
      case "plan.resumed": {
        const plan = { ...e.data.plan, run_status: "RUNNING" } as WorkPlan;
        setPlans((prev) => [plan, ...prev.filter((item) => item.id !== plan.id)]);
        activity(e.event === "plan.resumed"
          ? `Resumed ${plan.skill_id} work plan`
          : `Created ${plan.skill_id} work plan`);
        break;
      }
      case "tasks.created":
      case "tasks.updated":
        setPlans((prev) => prev.map((plan) => plan.id === e.data.plan_id
          ? { ...plan, tasks: e.data.tasks as WorkTask[] }
          : plan));
        break;
      case "task.updated":
        setPlans((prev) => prev.map((plan) => plan.id === e.data.task?.plan_id
          ? { ...plan, tasks: plan.tasks.map((task) => task.id === e.data.task.id
            ? e.data.task as WorkTask : task) }
          : plan));
        break;
      case "run.started":
        setRunId(e.data.run_id === "run_pending" ? null : e.data.run_id);
        setRunStatus("Getting ready to help…");
        // adopt the session created for this run so reloads find the history
        if (e.data.session_id) {
          setSessionId((cur) => {
            const sid = cur || e.data.session_id;
            sessionIdRef.current = sid;
            if (!activeRunSessionIdRef.current) activeRunSessionIdRef.current = sid;
            return sid;
          });
        }
        break;
      case "context.search.started":
        setRunStatus("Looking through project context…");
        break;
      case "context.search.completed":
        setRunStatus(e.data.sources?.length
          ? `Using ${e.data.sources.length} relevant source${e.data.sources.length === 1 ? "" : "s"}`
          : "No matching project sources found yet");
        if (e.data.sources?.length) activity(`Reviewed ${e.data.sources.join(", ")}`);
        break;
      case "context.compacting":
        setRunStatus("Context is getting long — compacting…");
        break;
      case "context.compacted":
        setRunStatus(
          `Context compacted (${e.data.before_tokens} → ${e.data.after_tokens} tokens)`);
        break;
      case "tool.started":
        setRunStatus(`Using ${e.data.tool}…`);
        activity(`Using ${e.data.tool}`);
        break;
      case "tool.completed":
        setRunStatus(`${e.data.tool}: ${e.data.summary || "done"}`);
        activity(`${e.data.tool}: ${e.data.summary || "done"}`);
        break;
      case "skill.started":
        setRunStatus(`Using the ${e.data.skill} skill…`);
        activity(`Started ${e.data.skill} skill`);
        break;
      case "skill.completed":
        setRunStatus(`Finished with the ${e.data.skill} skill`);
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
        activity(e.data.status === "SUCCEEDED" ? "Work completed" : `Work ${String(e.data.status).toLowerCase()}`);
        api.listArtifacts(pid).then((r) => setArtifacts(r.artifacts)).catch(() => {});
        if (sessionIdRef.current) api.workPlans(pid, sessionIdRef.current).then((r) => setPlans(r.plans)).catch(() => {});
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
        activity("Current work stopped; completed tasks were preserved");
        setRunning(false);
        setRunId(null);
        break;
    }
  }

  async function sendMessage(message: string) {
    setError("");
    setMessages((prev) => [...prev, {
      id: `user-${Date.now()}`, role: "user", content: message,
      meta: { selected_files: selectedFiles }, created_at: "",
    }]);
    setRunning(true);
    runningRef.current = true;
    setRunStatus("Reading your message…");
    try {
      await api.chat(pid, {
        session_id: activeRunSessionIdRef.current || sessionIdRef.current || sessionId || undefined,
        message,
        selected_files: selectedFiles,
      }, handleEvent);
      refreshSessions();
      window.dispatchEvent(new Event("cowork:sessions-refresh"));
      setMessages((prev) => prev.map((m) => ({ ...m, pending: false })));
    } catch (e) {
      setError(String(e));
      setRunning(false);
    } finally {
      runningRef.current = false;
      const next = queuedRef.current.shift();
      setQueuedInstructions([...queuedRef.current]);
      if (next) {
        setRunStatus("Sending your next instruction…");
        void sendMessage(next);
      } else {
        setRunning(false);
        activeRunSessionIdRef.current = null;
      }
    }
  }

  function send() {
    const message = input.trim();
    if (!message) return;
    setInput("");
    if (runningRef.current) {
      queuedRef.current.push(message);
      setQueuedInstructions([...queuedRef.current]);
      return;
    }
    activeRunSessionIdRef.current = sessionIdRef.current;
    void sendMessage(message);
  }

  async function cancel() {
    queuedRef.current = [];
    setQueuedInstructions([]);
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

  function planStatusLabel(status: string) {
    const labels: Record<string, string> = {
      RUNNING: "In progress", SUCCEEDED: "Completed", FAILED: "Failed",
      CANCELLED: "Stopped", WAITING_USER: "Needs your input",
    };
    return labels[status] || status.toLowerCase();
  }

  function skillInvocation(content: string) {
    const match = content.match(/^\/([A-Za-z0-9_-]+)\s*([\s\S]*)$/);
    if (!match) return null;
    const skill = skills.find((item) => item.id === match[1]);
    if (!skill) return null;
    return { skill, instruction: match[2].trim() || "Work with the project context" };
  }

  return (
    <>
      <div className="chatlog" ref={logRef}>
        {messages.length === 0 ? (
          <div className="workspace-heading"><div><div className="eyebrow">COWORK</div><h1>What would you like to work on?</h1><p>Describe a goal or ask a question. We can make a plan and refine it together as we work.</p></div><Badge variant="outline" className="context-count"><span className="live-dot" />{files.length} source {files.length === 1 ? "file" : "files"}</Badge></div>
        ) : (
          <div className="conversation-title"><div><span className="chat-title-icon">✳</span><div><strong>{sessions.find((s) => s.id === sessionId)?.title || "Project work"}</strong><small>Cowork session · {files.length} project files</small></div></div></div>
        )}
        {messages.length === 0 && (
          <Card className="empty-state"><CardContent className="empty-content"><div className="empty-icon"><Sparkles /></div><h2>Start a conversation</h2><p>We can explore the source together, make a plan, then work through it one step at a time.</p><div className="suggestions"><Button variant="outline" onClick={() => setInput("Can you walk me through the key points in my project files?")}>Explore project files <ArrowUp /></Button><Button variant="outline" onClick={() => setInput("Let’s review the source documents for gaps and open questions")}>Find gaps and questions <ArrowUp /></Button><Button variant="outline" onClick={() => setInput("/ba Let’s draft requirements from the project specification")}>Work on requirements with <code className="inline">/ba</code> <ArrowUp /></Button></div></CardContent></Card>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            <div className="who"><span className={m.role === "user" ? "user-mark" : "agent-mark"}>{m.role === "user" ? "Y" : "✳"}</span>{m.role === "user" ? "You" : "Cowork"}</div>
            {m.role === "user" && skillInvocation(m.content) ? (() => {
              const run = skillInvocation(m.content)!;
              return <Card className="skill-run-card"><div className="skill-run-icon"><Sparkles /></div><div><div className="eyebrow">SKILL RUN</div><strong>{run.skill.name}</strong><p>{run.instruction}</p><small>{files.length} project files available</small></div></Card>;
            })() : <div className="bubble">{m.content}</div>}
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
        {plans.map((plan) => {
          const completed = plan.tasks.filter((task) => task.status === "completed").length;
          const current = plan.tasks.find((task) => task.status === "running" || task.status === "needs_input" || task.status === "failed");
          return <Card key={plan.id} className="feed-plan-card">
            <div className="feed-plan-heading"><div><div className="eyebrow">{plan.skill_id} · PLAN</div><h2>{plan.goal}</h2></div><Badge variant={plan.run_status === "RUNNING" ? "secondary" : "outline"}>{planStatusLabel(plan.run_status)}</Badge></div>
            <p className="feed-plan-summary">{plan.summary}</p>
            <div className="plan-progress-label"><span>{current?.title || (completed === plan.tasks.length ? "Plan complete" : "Progress")}</span><span>{completed} / {plan.tasks.length}</span></div>
            <div className="plan-progress"><span style={{ width: `${plan.tasks.length ? completed / plan.tasks.length * 100 : 0}%` }} /></div>
            <ol className="feed-task-list">{plan.tasks.map((task) => <li key={task.id} className={`feed-task ${task.status}`}><span className="feed-task-mark">{task.status === "completed" ? "✓" : task.status === "running" ? "◐" : task.status === "needs_input" ? "!" : task.status === "failed" ? "×" : task.status === "skipped" ? "–" : "○"}</span><span>{task.title}{(task.status === "failed" || task.status === "needs_input") && task.error && <small className="task-inline-error">{task.error}</small>}</span>{task.status === "running" && <small>Working</small>}{task.status === "needs_input" && <small>Needs your input</small>}</li>)}</ol>
            {plan.sources.length > 0 && <div className="feed-plan-sources"><span>Sources</span>{plan.sources.map((source) => <Badge key={source} variant="outline">{source}</Badge>)}</div>}
          </Card>;
        })}
        {activities.length > 0 && <details className="activity-feed"><summary>Recent activity <span>{activities.length}</span></summary><ol>{activities.map((item) => <li key={item.id}>{item.text}</li>)}</ol></details>}
        {artifacts.length > 0 && <section className="feed-artifacts"><div className="feed-section-title">Recent artifacts <Link href={`/projects/${pid}/outputs`}>View all</Link></div>{artifacts.slice(0, 3).map((artifact) => <Link key={artifact.id} className="feed-artifact" href={`/projects/${pid}/outputs`}><span>▤</span><div><strong>{artifact.file_name}</strong><small>Version {artifact.current_version} · {artifact.validation_status}</small></div><span>→</span></Link>)}</section>}
        {runStatus && <Card className="run-card"><span className="run-indicator" /><div><strong>{running ? (plans.some((plan) => plan.run_status === "RUNNING") ? plans.find((plan) => plan.run_status === "RUNNING")?.tasks.find((task) => task.status === "running")?.title || "Working through the plan" : "Working on your request") : "Latest activity"}</strong><p>{runStatus}</p></div></Card>}
        {queuedInstructions.length > 0 && <Card className="queued-card"><strong>Next instructions · {queuedInstructions.length}</strong>{queuedInstructions.map((instruction, index) => <p key={`${index}-${instruction}`}>{instruction}</p>)}</Card>}
      </div>

      <div className="chatinput">
        <div className="attached-files">
          {selectedFiles.length > 0 && <span className="attach-label">Using</span>}
          {files.filter((f) => selectedFiles.includes(f.name)).map((f) => (
            <Badge key={f.id} variant="secondary" className="selected-file"><FileText />@{f.name}<Button variant="ghost" size="icon-xs" aria-label={`Remove ${f.name}`} onClick={() => toggleFile(f.name)}><X /></Button></Badge>
          ))}
        </div>
        <div className="composer">
          <Input
            type="text"
            className="composer-input"
            placeholder="Message your cowork…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
          />
          <div className="composer-actions">
            {running && <Button variant="destructive" onClick={cancel}><Square data-icon="inline-start" />Stop</Button>}
            <Button className="send-btn" onClick={send} disabled={!input.trim()}>{running ? "Queue instruction" : "Send"} <ArrowUp data-icon="inline-end" /></Button>
          </div>
        </div>
        <div className="composer-footer"><span>{running ? "Your instruction will be sent next in this session" : "Enter to send · Start with a goal or a skill"}</span><details className="file-picker-menu">
          <summary><Plus /> Files{selectedFiles.length > 0 ? ` · ${selectedFiles.length} selected` : ""}</summary>
          <div className="file-picker">
            {files.map((f) => (
              <Button key={f.id} variant={selectedFiles.includes(f.name) ? "secondary" : "outline"} size="xs" onClick={() => toggleFile(f.name)}>
                {selectedFiles.includes(f.name) ? <Check data-icon="inline-start" /> : <FileText data-icon="inline-start" />} {f.name}
              </Button>
            ))}
            {files.length === 0 && <span className="muted">No project files available</span>}
          </div>
        </details></div>
        {skills.length > 0 && <div className="conversation-context"><span>Available skills</span>{skills.map((skill) => <Badge key={skill.id} variant="secondary"><Sparkles />{skill.name}</Badge>)}</div>}
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
