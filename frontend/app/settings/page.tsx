"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, JevSettings, JevTestResult, LLMProfile } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

const EMPTY = {
  name: "", base_url: "", model: "", api_key: "", timeout_seconds: 120,
  tool_calling_mode: "auto", is_default: false,
};

interface OcrSettings {
  ocr_enabled: boolean;
  ocr_engine: string;
  engine_available: boolean;
  docling_enabled: boolean;
  docling_available: boolean;
  docling_version: string | null;
  effective_parser: string;
  marked_stale?: number;
}

export default function SettingsPage() {
  const [profiles, setProfiles] = useState<LLMProfile[]>([]);
  const [form, setForm] = useState<Record<string, any>>({ ...EMPTY });
  const [editing, setEditing] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, any> | null>(null);
  const [jev, setJev] = useState<JevSettings | null>(null);
  const [jevApiKey, setJevApiKey] = useState("");
  const [jevTest, setJevTest] = useState<JevTestResult | null>(null);
  const [jevBusy, setJevBusy] = useState(false);
  const [jevError, setJevError] = useState("");
  const [jevNotice, setJevNotice] = useState("");
  const [ocr, setOcr] = useState<OcrSettings | null>(null);
  const [limits, setLimits] = useState<Record<string, any> | null>(null);
  const [budgetDraft, setBudgetDraft] = useState<string>("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = useCallback(() => {
    api.profiles().then((r) => setProfiles(r.profiles)).catch((e) => setError(String(e)));
    api.jevSettings().then(setJev).catch((e) => setJevError(String(e)));
    api.processingSettings().then((r) => setOcr(r as OcrSettings)).catch(() => {});
    api.limits().then((r) => {
      setLimits(r);
      setBudgetDraft(String(r.daily_token_budget ?? 0));
    }).catch(() => {});
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  async function applyProcessing(body: Record<string, unknown>) {
    setError("");
    setNotice("");
    try {
      const res = (await api.saveProcessingSettings(body)) as OcrSettings;
      setOcr(res);
      if (res.marked_stale) {
        setNotice(
          `${res.marked_stale} indexed document(s) marked stale — reprocess them ` +
          "from the Files page to parse with the new parser.");
      }
    } catch (e) {
      setError(String(e));
    }
  }

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

  async function updateJev(body: { enabled?: boolean; api_key?: string; clear_api_key?: boolean }) {
    setJevBusy(true);
    setJevError("");
    setJevNotice("");
    setJevTest(null);
    try {
      const result = await api.saveJevSettings(body);
      setJev(result);
      if (body.api_key || body.clear_api_key) setJevApiKey("");
      setJevNotice(body.clear_api_key ? "TypeSafe API key removed." : "Jev settings saved.");
    } catch (e) {
      setJevError(String(e));
    } finally {
      setJevBusy(false);
    }
  }

  async function testJev() {
    setJevBusy(true);
    setJevError("");
    setJevNotice("");
    setJevTest(null);
    try {
      const result = await api.testJev();
      setJevTest(result);
      if (result.reachable) setJevNotice("TypeSafe connection verified.");
      else setJevError(result.error || "TypeSafe connection failed.");
    } catch (e) {
      setJevError(String(e));
    } finally {
      setJevBusy(false);
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

        <Card className="mb-4">
          <CardHeader>
            <div className="flex items-center justify-between gap-4">
              <div>
                <CardTitle>Internal tools · Jev</CardTitle>
                <CardDescription className="mt-1">
                  Optional request routing inside the harness. The main chat model still writes every response.
                </CardDescription>
              </div>
              {jev && (
                <span className={jev.enabled && jev.ready ? "pill ok" : "pill warn"}>
                  {jev.enabled && jev.ready ? "Enabled" : "Disabled"}
                </span>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-5">
            {jev ? (
              <>
                <div className="flex flex-wrap items-center justify-between gap-4 rounded-lg border p-4">
                  <div>
                    <div className="font-medium">Use Jev for chat routing</div>
                    <div className="text-sm text-muted-foreground">
                      Jev is currently {jev.enabled ? "on" : "off"}; it will only auto-select an enabled skill at high confidence.
                    </div>
                  </div>
                  <Switch
                    aria-label="Enable Jev for chat routing"
                    checked={jev.enabled}
                    disabled={jevBusy || !jev.has_api_key}
                    onCheckedChange={(enabled) => updateJev({ enabled })}
                  />
                </div>

                <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
                  <label className="grid gap-2 text-sm font-medium">
                    TypeSafe API key {jev.has_api_key && <span className="text-muted-foreground">(saved; leave blank to keep)</span>}
                    <Input
                      type="password"
                      autoComplete="new-password"
                      value={jevApiKey}
                      onChange={(event) => setJevApiKey(event.target.value)}
                      placeholder={jev.has_api_key ? "Key saved securely" : "Paste your TypeSafe API key"}
                    />
                  </label>
                  <div className="flex flex-wrap gap-2">
                    <Button disabled={jevBusy || !jevApiKey.trim()} onClick={() => updateJev({ api_key: jevApiKey.trim() })}>
                      Save key
                    </Button>
                    <Button variant="outline" disabled={jevBusy || !jev.has_api_key} onClick={testJev}>
                      Test connection
                    </Button>
                    {jev.has_api_key && (
                      <Button variant="destructive" disabled={jevBusy} onClick={() => updateJev({ clear_api_key: true })}>
                        Remove key
                      </Button>
                    )}
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                  <span>Model</span>
                  <code className="rounded bg-muted px-2 py-1 text-foreground">{jev.model}</code>
                  <span>· API key {jev.has_api_key ? "configured" : "not configured"}</span>
                </div>
                <p className="text-sm text-muted-foreground">
                  When enabled, the text you send in Chat is sent to TypeSafe for classification. The POC does not send document contents to Jev.
                </p>
                {jevTest && (
                  <div className={jevTest.reachable ? "rounded-lg border border-green-600/30 bg-green-50 p-3 text-sm" : "rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm"}>
                    {jevTest.reachable ? "Reachable" : "Not reachable"} · {jevTest.model} · {jevTest.latency_ms ?? "—"} ms
                    {jevTest.error && <div className="mt-1">{jevTest.error}</div>}
                  </div>
                )}
                {jevNotice && <div role="status" className="text-sm text-green-700">{jevNotice}</div>}
                {jevError && <div role="alert" className="text-sm text-destructive">{jevError}</div>}
              </>
            ) : (
              <div className="text-sm text-muted-foreground">Loading Jev settings…</div>
            )}
          </CardContent>
        </Card>

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
          <h2 style={{ marginTop: 0, fontSize: 16 }}>Agent Limits</h2>
          {limits && (
            <>
              <div className="muted" style={{ marginBottom: 8 }}>
                Tokens used today: <strong>{limits.tokens_used_today.toLocaleString()}</strong>
                {limits.daily_token_budget > 0 &&
                  ` / ${limits.daily_token_budget.toLocaleString()} budget`}
                {limits.daily_token_budget === 0 && " (no daily cap)"}
              </div>
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <label className="field" style={{ margin: 0, width: 220 }}>
                  <span>Daily token budget (0 = unlimited)</span>
                  <input type="number" defaultValue={limits.daily_token_budget} min={0}
                         onChange={(e) => setBudgetDraft(e.target.value)} />
                </label>
                <button className="btn secondary" style={{ marginTop: 16 }}
                        onClick={async () => setLimits(await api.saveLimits({
                          daily_token_budget: Number(budgetDraft || 0)}))}>
                  Save budget
                </button>
                <button className={limits.kill_switch ? "btn danger" : "btn"}
                        style={{ marginTop: 16 }}
                        onClick={async () =>
                          setLimits(await api.saveLimits({ kill_switch: !limits.kill_switch }))}>
                  {limits.kill_switch ? "▶ Resume agent runs" : "⏸ Kill switch: pause agent runs"}
                </button>
              </div>
              {limits.kill_switch && (
                <div className="error-text" style={{ marginTop: 8 }}>
                  Kill switch is ACTIVE — agent runs are refused until resumed.
                </div>
              )}
            </>
          )}
        </div>

        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>Processing</h2>
          {ocr && (
            <>
              <h3 style={{ fontSize: 13, margin: "12px 0 6px" }}>Document parser (PDF / DOCX / PPTX)</h3>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <input
                  type="checkbox"
                  style={{ width: "auto" }}
                  checked={ocr.docling_enabled}
                  onChange={async (e) =>
                    applyProcessing({ docling_enabled: e.target.checked })}
                />
                <span style={{ flex: 1 }}>
                  <strong>Docling</strong> — high-quality layout &amp; table parsing{" "}
                  {ocr.docling_available ? (
                    <span className="pill ok">
                      ready{ocr.docling_version ? ` · v${ocr.docling_version}` : ""}
                    </span>
                  ) : (
                    <span className="pill warn">not installed</span>
                  )}
                </span>
              </div>
              <div className="muted" style={{ marginTop: 4 }}>
                {ocr.docling_available
                  ? ocr.docling_enabled
                    ? `Active parser: ${ocr.effective_parser}. Newly uploaded and reprocessed files use Docling.`
                    : "Installed but disabled — the built-in parsers are used."
                  : "To enable: install it with  pip install docling  in the backend environment, then restart. Until then the built-in lightweight parsers are used (works offline, no model downloads)."}
              </div>

              <h3 style={{ fontSize: 13, margin: "12px 0 6px" }}>OCR</h3>
              <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <input type="checkbox" style={{ width: "auto" }} checked={ocr.ocr_enabled}
                       onChange={async (e) =>
                         applyProcessing({ ocr_enabled: e.target.checked })} />
                <span className="muted">
                  Enable OCR for scanned documents ({ocr.ocr_engine}
                  {ocr.engine_available ? ", installed" : ", not installed on this machine"})
                </span>
              </label>
              {notice && <div className="ok-text" style={{ marginTop: 8 }}>{notice}</div>}
            </>
          )}
        </div>
      </main>
    </>
  );
}
