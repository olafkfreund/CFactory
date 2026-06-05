import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { fetchAnomalies, fetchRollups, fetchTokens, type Anomaly, type Rollups, type TokenTotals, type WorkItem } from "./api";
import { useCountUp } from "./motion";

const NODES = [
  { key: "pfactory", label: "Plan", svc: "PFactory", color: "var(--plan)", cx: 110 },
  { key: "aifactory", label: "Code", svc: "AIFactory", color: "var(--code)", cx: 320 },
  { key: "tfactory", label: "Test", svc: "TFactory", color: "var(--test)", cx: 530 },
] as const;
const CONNECTORS = [
  { from: 0, x1: 162, x2: 268, color: "var(--plan)" },
  { from: 1, x1: 372, x2: 478, color: "var(--code)" },
];

function furthest(wi: WorkItem): "pfactory" | "aifactory" | "tfactory" {
  if (wi.tfactory.status) return "tfactory";
  if (wi.aifactory.status) return "aifactory";
  return "pfactory";
}

function fmtDur(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

export default function MissionControl({ items, reloadSignal }: { items: WorkItem[]; reloadSignal: number }) {
  const reduced = useReducedMotion();
  const [rollups, setRollups] = useState<Rollups | null>(null);
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);
  const [tokens, setTokens] = useState<TokenTotals | null>(null);

  useEffect(() => {
    fetchRollups().then(setRollups).catch(() => setRollups(null));
    fetchAnomalies().then(setAnomalies).catch(() => setAnomalies([]));
    fetchTokens().then(setTokens).catch(() => setTokens(null));
  }, [reloadSignal]);

  const counts = useMemo(() => {
    const c = { pfactory: 0, aifactory: 0, tfactory: 0 };
    for (const wi of items) c[furthest(wi)]++;
    return c;
  }, [items]);

  const totalEvents = rollups?.total_events ?? 0;

  return (
    <>
      <div className="page-head">
        <h1>Mission Control</h1>
        <p>The whole factory at a glance — plan → code → test, live.</p>
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

      <section className="mc-flow">
        <svg viewBox="0 0 640 180" className="mc-svg" role="img" aria-label="Pipeline flow">
          {CONNECTORS.map((c, i) => (
            <g key={i}>
              <line x1={c.x1} y1={90} x2={c.x2} y2={90} stroke="var(--border)" strokeWidth={2} strokeDasharray="4 5" />
              {!reduced &&
                [0, 0.8].map((delay, j) => (
                  <motion.circle
                    key={j}
                    r={3.2}
                    cy={90}
                    fill={c.color}
                    initial={{ cx: c.x1, opacity: 0 }}
                    animate={{ cx: [c.x1, c.x2], opacity: [0, counts[NODES[c.from].key] ? 1 : 0.25, 0] }}
                    transition={{ duration: 1.6, repeat: Infinity, ease: "linear", delay }}
                  />
                ))}
            </g>
          ))}
          {NODES.map((n) => {
            const active = counts[n.key] > 0;
            return (
              <g key={n.key}>
                {active && !reduced && (
                  <motion.circle
                    cx={n.cx} cy={90} r={46} fill="none" stroke={n.color} strokeWidth={2}
                    initial={{ opacity: 0.5, scale: 1 }}
                    animate={{ opacity: [0.5, 0, 0.5], scale: [1, 1.25, 1] }}
                    transition={{ duration: 2.4, repeat: Infinity, ease: "easeOut" }}
                    style={{ transformOrigin: `${n.cx}px 90px` }}
                  />
                )}
                <circle cx={n.cx} cy={90} r={42} fill="var(--panel)" stroke={n.color} strokeWidth={active ? 2.5 : 1.5} opacity={active ? 1 : 0.6} />
                <text x={n.cx} y={86} textAnchor="middle" className="mc-node-count" fill="var(--text)">{counts[n.key]}</text>
                <text x={n.cx} y={104} textAnchor="middle" className="mc-node-lbl" fill={n.color}>{n.label}</text>
                <text x={n.cx} y={150} textAnchor="middle" className="mc-node-svc" fill="var(--faint)">{n.svc}</text>
              </g>
            );
          })}
        </svg>
      </section>

      <div className="mc-grid">
        <div className="mc-panel">
          <h2 className="panel-title">Anomalies</h2>
          {anomalies.length === 0 ? (
            <div className="card card--ok">No anomalies 🎉</div>
          ) : (
            <AnimatePresence initial={false}>
              {anomalies.map((a, i) => (
                <motion.div
                  key={`${a.correlation_key}-${a.kind}-${i}`}
                  className={`card card--${a.severity}`}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.25 }}
                >
                  <div className="card-head">
                    <span className="card-kind">{a.kind}</span>
                    <span className="card-key">#{a.correlation_key}</span>
                  </div>
                  <div className="card-detail">{a.detail}</div>
                </motion.div>
              ))}
            </AnimatePresence>
          )}
        </div>

        <div className="mc-panel">
          <h2 className="panel-title">Live agents</h2>
          <div className="mc-agents">
            {[0, 1, 2].map((i) => (
              <div className="mc-agent-ph" key={i}>
                <span className="mc-agent-dot" />
                rmux live view
                <span className="mc-agent-sub">Phase 5</span>
              </div>
            ))}
          </div>
          <p className="mc-note">Live agent terminals (AIFactory rmux) arrive in Phase 5.</p>
        </div>
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
