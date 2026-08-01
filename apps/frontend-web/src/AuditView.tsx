import { useEffect, useState } from "react";
import { fetchActivity, fetchAudit, type ActivityEntry, type AuditEntry } from "./api";
import { keySlug } from "./correlationKey";

function rel(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/** The exact instant, in UTC, for the row tooltip (#258).
 *
 * `rel()` above is now correct because the backend serialises an offset, but
 * "60m ago" is not an audit record. A compliance reader needs the instant, and
 * needs it in one zone rather than in whichever one their laptop is set to. */
function absUtc(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : `${d.toISOString().replace("T", " ").slice(0, 19)} UTC`;
}

export default function AuditView({ reloadSignal }: { reloadSignal: number }) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetchActivity()
      .then(setActivity)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
    fetchAudit()
      .then((e) => { setEntries(e); setErr(null); })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  }, [reloadSignal]);

  return (
    <>
      <div className="page-head">
        <h1>Audit</h1>
        <p>Live pipeline activity, plus every confirmed action executed against an upstream (HMAC-chained).</p>
      </div>
      {err && <div className="banner banner--error">{err}</div>}

      <h2 className="panel-title">Activity</h2>
      <p className="mc-note">Recent completion events across all work items.</p>
      <div className="table">
        <div className="table-head">
          <span>SERVICE</span><span>WORK ITEM</span><span>STATUS</span><span>PHASE</span><span className="ta-r">TIME</span>
        </div>
        {activity.length === 0 ? (
          <div className="table-empty">No activity yet — events appear here as work flows through the pipeline</div>
        ) : (
          activity.map((a, i) => (
            <div className="table-row" key={`${a.correlation_key}-${a.service}-${a.updated_at}-${i}`}>
              <span className={`t-svc t-svc--${a.service}`}><span className="t-svc-dot" /> {a.service}</span>
              <span className="t-wi">
                <span className="t-key" title={a.correlation_key}>#{keySlug(a.correlation_key)}</span>
                {a.title && <span className="t-wi-title"> {a.title}</span>}
              </span>
              <span className="t-kind">{a.status}</span>
              <span className="t-target mono">{a.phase ?? "—"}</span>
              <span className="t-time ta-r" title={absUtc(a.updated_at)}>{rel(a.updated_at)}</span>
            </div>
          ))
        )}
      </div>

      <h2 className="panel-title audit-actions-title">Confirmed actions</h2>
      <p className="mc-note">Human-in-the-loop write actions executed against an upstream.</p>
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
                <span className="t-key" title={e.correlation_key}>#{keySlug(e.correlation_key)}</span>
              </span>
              <span className="t-target mono">{e.target_service}{e.endpoint}</span>
              <span>
                <span className={`status-pill ${e.ok ? "ok" : "fail"}`}>
                  <span className="dot" /> {e.ok ? "ok" : "fail"} {e.status_code}
                </span>
              </span>
              <span className="t-actor mono">{e.actor}</span>
              <span className="t-time ta-r" title={absUtc(e.ts)}>{rel(e.ts)}</span>
            </div>
          ))
        )}
      </div>
    </>
  );
}
