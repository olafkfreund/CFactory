import { useEffect, useState } from "react";
import { fetchAudit, type AuditEntry } from "./api";

function rel(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export default function AuditView({ reloadSignal }: { reloadSignal: number }) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetchAudit()
      .then((e) => { setEntries(e); setErr(null); })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  }, [reloadSignal]);

  return (
    <>
      <div className="page-head">
        <h1>Audit</h1>
        <p>Every confirmed action executed against an upstream service, HMAC-chained.</p>
      </div>
      {err && <div className="banner banner--error">{err}</div>}
      <div className="table">
        <div className="table-head">
          <span>ACTION</span><span>TARGET</span><span>RESULT</span><span>ACTOR</span><span className="ta-r">TIME</span>
        </div>
        {entries.length === 0 ? (
          <div className="table-empty">No actions executed yet</div>
        ) : (
          entries.map((e) => (
            <div className="table-row" key={e.id}>
              <span className="t-action">
                <span className="t-kind">{e.kind}</span>
                <span className="t-key">#{e.correlation_key}</span>
              </span>
              <span className="t-target mono">{e.target_service}{e.endpoint}</span>
              <span>
                <span className={`status-pill ${e.ok ? "ok" : "fail"}`}>
                  <span className="dot" /> {e.ok ? "ok" : "fail"} {e.status_code}
                </span>
              </span>
              <span className="t-actor mono">{e.actor}</span>
              <span className="t-time ta-r">{rel(e.ts)}</span>
            </div>
          ))
        )}
      </div>
    </>
  );
}
