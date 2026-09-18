export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export interface Project {
  id: string;
  name: string;
  description: string;
  instruction: string;
  status: string;
  created_at: string;
  file_count?: number;
  ready_count?: number;
}

export interface ProjectFile {
  id: string;
  name: string;
  kind: string;
  size_bytes: number;
  status: string;
  error: string | null;
  created_at: string;
}

export interface Session {
  id: string;
  title: string;
  updated_at: string;
}

export interface MemoryItem {
  id: string;
  content: string;
  kind: string;
  source: string;
  status: string;
  source_refs: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: string;
  role: string;
  content: string;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface Artifact {
  id: string;
  file_name: string;
  skill_id: string | null;
  validation_status: string;
  current_version: number;
  updated_at: string;
}

export interface ArtifactDetail extends Artifact {
  versions: { version: number; created_by: string; created_at: string; validation: Record<string, unknown> }[];
}

export interface Skill {
  id: string;
  name: string;
  version: string;
  builtin: boolean;
  enabled: boolean;
  enabled_by_default: boolean;
  description: string;
}

export interface Entity {
  id: string;
  type: string;
  canonical_name: string;
  aliases: string[];
  degree: number;
}

export interface Relation {
  id: string;
  relation_type: string;
  source: string;
  target: string;
  confidence: number;
  evidence: { document_id: string; chunk_id: string; page: number | null; text: string }[];
}

export interface ReviewItem {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
  suggestion: Record<string, unknown>;
  status: string;
}

export interface Evidence {
  chunk_id: string;
  document: string;
  document_id: string;
  page: number | null;
  section_path: string[];
  text: string;
}

export interface LLMProfile {
  id: string;
  name: string;
  base_url: string;
  model: string;
  is_default: boolean;
  has_api_key: boolean;
  tool_calling_mode: string;
  timeout_seconds: number;
}

export interface RunEvent {
  event: string;
  data: Record<string, any>;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {}
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  // projects
  listProjects: () => req<{ projects: Project[] }>("/api/projects"),
  createProject: (body: { name: string; description?: string; instruction?: string }) =>
    req<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),
  getProject: (id: string) => req<Project>(`/api/projects/${id}`),
  deleteProject: (id: string) => req<void>(`/api/projects/${id}`, { method: "DELETE" }),

  // files
  listFiles: (pid: string) => req<{ files: ProjectFile[] }>(`/api/projects/${pid}/files`),
  uploadFiles: async (pid: string, files: File[]) => {
    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    const res = await fetch(`${API_BASE}/api/projects/${pid}/files`, { method: "POST", body: form });
    if (!res.ok) throw new Error((await res.json()).detail || "Upload failed");
    return res.json() as Promise<{ files: { file_id: string; name: string; status: string; reused?: boolean }[] }>;
  },
  deleteFile: (pid: string, fid: string) =>
    req<void>(`/api/projects/${pid}/files/${fid}`, { method: "DELETE" }),
  reprocessFile: (pid: string, fid: string) =>
    req<{ job_id: string }>(`/api/projects/${pid}/files/${fid}/reprocess`, { method: "POST" }),
  processing: (pid: string) =>
    req<{ files: ProjectFile[]; active_jobs: number; latest_jobs: any[] }>(`/api/projects/${pid}/processing`),

  // sessions + messages
  listSessions: (pid: string) => req<{ sessions: Session[] }>(`/api/projects/${pid}/sessions`),
  createSession: (pid: string, title?: string) =>
    req<Session>(`/api/projects/${pid}/sessions`, { method: "POST", body: JSON.stringify({ title }) }),
  renameSession: (pid: string, sid: string, title: string) =>
    req<Session>(`/api/projects/${pid}/sessions/${sid}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  deleteSession: (pid: string, sid: string) =>
    req<void>(`/api/projects/${pid}/sessions/${sid}`, { method: "DELETE" }),
  listMessages: (pid: string, sid: string) =>
    req<{ messages: Message[] }>(`/api/projects/${pid}/sessions/${sid}/messages`),

  // chat streaming — returns parsed SSE events via callback
  async chat(
    pid: string,
    body: { session_id?: string; message: string; selected_files?: string[]; skill_id?: string | null; harness?: string },
    onEvent: (e: RunEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const res = await fetch(`${API_BASE}/api/projects/${pid}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok || !res.body) throw new Error((await res.json()).detail || "Chat failed");
    await consumeSSE(res.body, onEvent);
  },
  cancelRun: (pid: string, runId: string) =>
    req<{ cancelled: boolean }>(`/api/projects/${pid}/runs/${runId}/cancel`, { method: "POST" }),

  // artifacts
  listArtifacts: (pid: string) => req<{ artifacts: Artifact[] }>(`/api/projects/${pid}/artifacts`),
  getArtifact: (pid: string, aid: string) => req<ArtifactDetail>(`/api/projects/${pid}/artifacts/${aid}`),
  artifactContent: (pid: string, aid: string, version?: number) =>
    req<{ content: string; version: number }>(
      `/api/projects/${pid}/artifacts/${aid}/content${version ? `?version=${version}` : ""}`),
  editArtifact: (pid: string, aid: string, content: string) =>
    req<ArtifactDetail>(`/api/projects/${pid}/artifacts/${aid}`, { method: "PUT", body: JSON.stringify({ content }) }),
  artifactEvidence: (pid: string, aid: string) =>
    req<{ citations: Evidence[]; unresolved: { chunk_id: string }[] }>(`/api/projects/${pid}/artifacts/${aid}/evidence`),

  // skills
  projectSkills: (pid: string) => req<{ skills: Skill[] }>(`/api/projects/${pid}/skills`),
  toggleSkill: (pid: string, sid: string, enabled: boolean) =>
    req<Skill>(`/api/projects/${pid}/skills/${sid}/enabled`, { method: "PUT", body: JSON.stringify({ enabled }) }),
  runSkill: async (
    pid: string, sid: string, body: { instruction: string; selected_files?: string[]; session_id?: string },
    onEvent: (e: RunEvent) => void,
  ) => {
    const res = await fetch(`${API_BASE}/api/projects/${pid}/skills/${sid}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "Skill run failed");
    if (!res.body) throw new Error("Empty response body");
    await consumeSSE(res.body, onEvent);
  },

  // memories (AI long-term memory)
  memories: (pid: string, includeArchived = false) =>
    req<{ memories: MemoryItem[] }>(
      `/api/projects/${pid}/memories${includeArchived ? "?include_archived=true" : ""}`),
  createMemory: (pid: string, body: { content: string; kind?: string }) =>
    req<MemoryItem>(`/api/projects/${pid}/memories`, { method: "POST", body: JSON.stringify(body) }),
  updateMemory: (pid: string, mid: string, body: { content?: string; kind?: string; status?: string }) =>
    req<MemoryItem>(`/api/projects/${pid}/memories/${mid}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteMemory: (pid: string, mid: string) =>
    req<void>(`/api/projects/${pid}/memories/${mid}`, { method: "DELETE" }),

  // knowledge
  entities: (pid: string, query?: string) =>
    req<{ entities: Entity[] }>(`/api/projects/${pid}/entities${query ? `?query=${encodeURIComponent(query)}` : ""}`),
  relations: (pid: string) => req<{ relations: Relation[] }>(`/api/projects/${pid}/relations`),
  review: (pid: string) => req<{ items: ReviewItem[] }>(`/api/projects/${pid}/review`),
  resolveReview: (pid: string, id: string, action: string) =>
    req<any>(`/api/projects/${pid}/review/${id}/resolve`, { method: "POST", body: JSON.stringify({ action }) }),
  conflicts: (pid: string) => req<{ conflicts: any[] }>(`/api/projects/${pid}/conflicts`),
  health: (pid: string) => req<Record<string, number>>(`/api/projects/${pid}/health`),
  evidence: (pid: string, chunkId: string) => req<Evidence>(`/api/projects/${pid}/evidence/${chunkId}`),

  // settings
  profiles: () => req<{ profiles: LLMProfile[] }>("/api/settings/llm-profiles"),
  saveProfile: (body: Record<string, unknown>, id?: string) =>
    req<LLMProfile>(`/api/settings/llm-profiles${id ? `/${id}` : ""}`, {
      method: id ? "PUT" : "POST", body: JSON.stringify(body),
    }),
  deleteProfile: (id: string) => req<void>(`/api/settings/llm-profiles/${id}`, { method: "DELETE" }),
  testProfile: (id: string) =>
    req<Record<string, any>>(`/api/settings/llm-profiles/${id}/test`, { method: "POST" }),
  processingSettings: () => req<Record<string, any>>("/api/settings/processing"),
  saveProcessingSettings: (body: { ocr_enabled: boolean; ocr_engine?: string }) =>
    req<Record<string, any>>("/api/settings/processing", { method: "PUT", body: JSON.stringify(body) }),
};

export async function consumeSSE(body: ReadableStream<Uint8Array>, onEvent: (e: RunEvent) => void) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() || "";
    for (const block of blocks) {
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) {
        try {
          onEvent({ event, data: JSON.parse(data) });
        } catch {}
      }
    }
  }
}
