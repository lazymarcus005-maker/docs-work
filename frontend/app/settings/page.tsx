"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, LLMProfile } from "@/lib/api";

const EMPTY = {
  name: "", base_url: "", model: "", api_key: "", timeout_seconds: 120,
  tool_calling_mode: "auto", is_default: false,
};

interface OcrSettings {
  ocr_enabled: boolean;
  ocr_engine: string;
  engine_available: boolean;
}

export default function SettingsPage() {
  const [profiles, setProfiles] = useState<LLMProfile[]>([]);
  const [form, setForm] = useState<Record<string, any>>({ ...EMPTY });
  const [editing, setEditing] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, any> | null>(null);
  const [ocr, setOcr] = useState<OcrSettings | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = useCallback(() => {
    api.profiles().then((r) => setProfiles(r.profiles)).catch((e) => setError(String(e)));
    api.processingSettings().then((r) => setOcr(r as OcrSettings)).catch(() => {});
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  async function save() {
    setError("");
    setNotice("");
    try {
      const body: Record<string, unknown> = { ...form };
      if (!body.api_key) delete body.api_key;
      await api.saveProfile(body, editing || undefined);
      setForm({ ...EMPTY });
      setEditing(null);
      setTestResult(null);
      setNotice("Profile saved. Use Test Connection to verify reachability.");
      refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  async function test(id: string) {
    setTestResult(null);
    setError("");
    try {
      setTestResult(await api.testProfile(id));
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <>
      <header className="topbar">
        <Link href="/">← Projects</Link>
        <span className="brand">Settings</span>
      </header>
      <main className="container" style={{ maxWidth: 860 }}>
        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>LLM Profiles</h2>
          <p className="muted">
            Connects to any OpenAI-compatible endpoint — a LiteLLM Gateway,
            a local model server, or a remote gateway. Model names are passed
            through as gateway aliases. API keys are stored write-only and
            never displayed again.
          </p>
          <table className="plain">
            <thead><tr><th>Name</th><th>Base URL</th><th>Model</th><th></th><th></th></tr></thead>
            <tbody>
              {profiles.map((p) => (
                <tr key={p.id}>
                  <td><strong>{p.name}</strong> {p.is_default && <span className="pill ok">default</span>}</td>
                  <td className="muted">{p.base_url}</td>
                  <td><code className="inline">{p.model}</code></td>
                  <td>
                    <button className="btn small secondary" onClick={async () => {
                      setEditing(p.id);
                      setForm({ ...EMPTY, name: p.name, base_url: p.base_url, model: p.model,
                                tool_calling_mode: p.tool_calling_mode,
                                timeout_seconds: p.timeout_seconds, is_default: p.is_default });
                    }}>Edit</button>{" "}
                    <button className="btn small secondary" onClick={async () => {
                      await api.saveProfile({ ...p, is_default: true }, p.id); refresh();
                    }}>Set default</button>{" "}
                    <button className="btn small" onClick={() => test(p.id)}>Test</button>{" "}
                    <button className="btn small danger" onClick={async () => {
                      await api.deleteProfile(p.id); refresh();
                    }}>Delete</button>
                  </td>
                  <td></td>
                </tr>
              ))}
              {profiles.length === 0 && (
                <tr><td colSpan={5} className="muted">No LLM profiles yet — add one below to enable chat.</td></tr>
              )}
            </tbody>
          </table>
          {testResult && (
            <div className="card" style={{ background: "#fafafa", marginTop: 10 }}>
              <strong>Test Connection</strong>
              <div className="muted">
                Reachable: {String(testResult.reachable)} · Auth: {String(testResult.authentication || "—")} ·
                Model callable: {String(testResult.model_callable)} · Latency: {testResult.latency_ms ?? "—"} ms
                {testResult.error && <div className="error-text">{testResult.error}</div>}
              </div>
            </div>
          )}
        </div>

        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>
            {editing ? "Edit LLM Profile" : "Add LLM Profile"}
          </h2>
          <div className="grid2">
            <label className="field"><span>Name</span>
              <input type="text" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
            <label className="field"><span>Model (gateway alias)</span>
              <input type="text" value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })}
                     placeholder="claude-sonnet / gpt-4o / local-alias" /></label>
            <label className="field"><span>Base URL / Endpoint</span>
              <input type="text" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                     placeholder="http://llm-gateway.local:4000/v1" /></label>
            <label className="field"><span>API Key {editing && "(leave blank to keep)"}</span>
              <input type="password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} /></label>
            <label className="field"><span>Timeout (seconds)</span>
              <input type="number" value={form.timeout_seconds}
                     onChange={(e) => setForm({ ...form, timeout_seconds: Number(e.target.value) })} /></label>
            <label className="field"><span>Tool calling mode</span>
              <select value={form.tool_calling_mode}
                      onChange={(e) => setForm({ ...form, tool_calling_mode: e.target.value })}>
                <option value="auto">auto</option>
                <option value="native">native</option>
                <option value="prompt-json">prompt-json</option>
              </select></label>
          </div>
          <label style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 10 }}>
            <input type="checkbox" style={{ width: "auto" }} checked={!!form.is_default}
                   onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />
            <span className="muted">Set as default profile</span>
          </label>
          <button className="btn" onClick={save}>{editing ? "Save changes" : "Add profile"}</button>
          {editing && (
            <button className="btn secondary" style={{ marginLeft: 8 }} onClick={() => {
              setEditing(null); setForm({ ...EMPTY });
            }}>Cancel</button>
          )}
          {notice && <div className="ok-text" style={{ marginTop: 8 }}>{notice}</div>}
          {error && <div className="error-text" style={{ marginTop: 8 }}>{error}</div>}
        </div>

        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>Processing</h2>
          {ocr && (
            <>
              <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <input type="checkbox" style={{ width: "auto" }} checked={ocr.ocr_enabled}
                       onChange={async (e) =>
                         setOcr((await api.saveProcessingSettings({ ocr_enabled: e.target.checked })) as OcrSettings)} />
                <span className="muted">
                  Enable OCR for scanned documents ({ocr.ocr_engine}
                  {ocr.engine_available ? ", installed" : ", not installed on this machine"})
                </span>
              </label>
            </>
          )}
        </div>
      </main>
    </>
  );
}
