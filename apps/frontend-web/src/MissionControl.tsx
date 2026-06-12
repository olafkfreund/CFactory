import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { fetchAnomalies, fetchRollups, fetchTokens, type Anomaly, type Rollups, type TokenTotals, type WorkItem } from "./api";
import { useCountUp } from "./motion";
import { keySlug } from "./correlationKey";
import LiveAgents from "./LiveAgents";

// Mission Control is the alarm + activity surface: stats, anomalies and live
// agents. The three-node PARR diagram moved to the persistent pipeline strip
// (PipelineStrip.tsx) under the topbar, where it's visible from every view.

function fmtDur(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

function severityIcon(sev: string) {
  if (sev === "high")
    return <path d="M12 3 L22 20 H2 Z M12 9 v5 M12 17 h.01" />;
  if (sev === "medium")
    return <g><circle cx="12" cy="12" r="9" /><path d="M12 7 v6 M12 16 h.01" /></g>;
  return <g><circle cx="12" cy="12" r="9" /><path d="M12 11 v5 M12 8 h.01" /></g>;
}

export default function MissionControl({ items, reloadSignal }: { items: WorkItem[]; reloadSignal: number }) {
  const [rollups, setRollups] = useState<Rollups | null>(null);
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);
  const [tokens, setTokens] = useState<TokenTotals | null>(null);

  useEffect(() => {
    fetchRollups().then(setRollups).catch(() => setRollups(null));
    fetchAnomalies().then(setAnomalies).catch(() => setAnomalies([]));
    fetchTokens().then(setTokens).catch(() => setTokens(null));
  }, [reloadSignal]);

  const totalEvents = rollups?.total_events ?? 0;

  return (
    <>
      <div className="page-head">
        <h1>Mission Control</h1>
        <p>Alarms and live activity — the pipeline strip above tracks plan → code → test from every view.</p>
      </div>

      <div className="mc-stats">
        <Stat label="Work items" value={items.length} accent="var(--cyan)" />
        <Stat label="Events" value={totalEvents} accent="var(--violet)" />
        <Stat label="Anomalies" value={anomalies.length} accent={anomalies.length ? "var(--red)" : "var(--green)"} />
        <StatText label="Avg latency" text={fmtDur(rollups?.latency?.avg_seconds)} accent="var(--muted)" />
        <StatText
          label="Cost (USD)"
          text={tokens && tokens.total.cost_usd > 0 ? `$${tokens.total.cost_usd.toFixed(2)}` : "—"}
          sub={tokens && tokens.total.total_tokens > 0 ? undefined : "awaiting usage"}
          accent="var(--code)"
        />
      </div>

      <div className="mc-grid">
        <div className="mc-panel">
          <div className="panel-title-row">
            <h2 className="panel-title">Anomalies</h2>
            {anomalies.length > 0 && <span className="anomaly-chip">{anomalies.length}</span>}
          </div>
          {anomalies.length === 0 ? (
            <motion.div className="anomaly-clear" initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }}>
              <svg className="anomaly-clear-icn" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
                <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="1.8" />
                <path d="M8 12.5l2.5 2.5L16 9" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span>All clear — no anomalies</span>
            </motion.div>
          ) : (
            <AnimatePresence initial={false}>
              {anomalies.map((a, i) => (
                <motion.div
                  key={`${a.correlation_key}-${a.kind}-${i}`}
                  className={`anomaly anomaly--${a.severity}`}
                  initial={{ opacity: 0, x: 10 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, height: 0, marginBottom: 0 }}
                  transition={{ duration: 0.25 }}
                >
                  <span className="anomaly-bar" />
                  <svg
                    className="anomaly-icn"
                    viewBox="0 0 24 24" width="16" height="16"
                    fill="none" stroke="currentColor" strokeWidth="1.8"
                    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
                  >
                    {severityIcon(a.severity)}
                  </svg>
                  <div className="anomaly-body">
                    <div className="anomaly-head">
                      <span className="anomaly-kind">{a.kind}</span>
                      <span className="anomaly-sev">{a.severity}</span>
                      <span className="anomaly-key" title={a.correlation_key}>#{keySlug(a.correlation_key)}</span>
                    </div>
                    <div className="anomaly-detail">{a.detail}</div>
                  </div>
                </motion.div>
              ))}
            </AnimatePresence>
          )}
        </div>

        <LiveAgents reloadSignal={reloadSignal} />
      </div>
    </>
  );
}

function Stat({ label, value, accent }: { label: string; value: number; accent: string }) {
  const n = useCountUp(value);
  return (
    <div className="mc-stat">
      <div className="mc-stat-v" style={{ color: accent }}>{n}</div>
      <div className="mc-stat-l">{label}</div>
    </div>
  );
}

function StatText({ label, text, sub, accent }: { label: string; text: string; sub?: string; accent: string }) {
  return (
    <div className="mc-stat">
      <div className="mc-stat-v" style={{ color: accent }}>{text}</div>
      <div className="mc-stat-l">{label}{sub && <span className="mc-stat-sub"> · {sub}</span>}</div>
    </div>
  );
}
