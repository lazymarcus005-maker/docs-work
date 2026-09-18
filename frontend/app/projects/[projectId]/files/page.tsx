"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, ProjectFile } from "@/lib/api";

const STATUS_PILL: Record<string, string> = {
  READY: "ok", FAILED: "err", STALE: "warn",
};

export default function FilesPage() {
  const params = useParams();
  const pid = String(params.projectId);
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [processing, setProcessing] = useState<{ active_jobs: number } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    api.listFiles(pid).then((r) => setFiles(r.files)).catch((e) => setError(String(e)));
    api.processing(pid).then(setProcessing).catch(() => {});
  }, [pid]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 1500); // processing status stays live (§28)
    return () => clearInterval(t);
  }, [refresh]);

  async function upload(fileList: FileList | null) {
    if (!fileList?.length) return;
    setBusy(true);
    setError("");
    try {
      await api.uploadFiles(pid, Array.from(fileList));
      refresh();
    } catch (e) {
      setError(String(e));
    }
    setBusy(false);
  }

  return (
    <div className="chatlog" style={{ overflowY: "auto" }}>
      <div style={{ maxWidth: 860, margin: "0 auto" }}>
        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>Project Files</h2>
          <input type="file" multiple disabled={busy}
                 onChange={(e) => upload(e.target.files)} />
          {busy && <div className="muted">Uploading…</div>}
          {error && <div className="error-text">{error}</div>}
          {processing && processing.active_jobs > 0 && (
            <div className="muted" style={{ marginTop: 8 }}>
              ⚙ {processing.active_jobs} background job(s) running…
            </div>
          )}
        </div>

        <div className="card">
          <table className="plain">
            <thead>
              <tr><th>File</th><th>Status</th><th>Size</th><th></th></tr>
            </thead>
            <tbody>
              {files.map((f) => (
                <tr key={f.id}>
                  <td>{f.name}</td>
                  <td><span className={`pill ${STATUS_PILL[f.status] || ""}`}>{f.status}</span></td>
                  <td className="muted">{(f.size_bytes / 1024).toFixed(1)} KB</td>
                  <td style={{ textAlign: "right" }}>
                    <button className="btn small secondary" onClick={async () => {
                      await api.reprocessFile(pid, f.id); refresh();
                    }}>Reprocess</button>{" "}
                    <button className="btn small danger" onClick={async () => {
                      await api.deleteFile(pid, f.id); refresh();
                    }}>Delete</button>
                    {f.error && <div className="error-text">{f.error}</div>}
                  </td>
                </tr>
              ))}
              {files.length === 0 && (
                <tr><td colSpan={4} className="muted">No files uploaded yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
