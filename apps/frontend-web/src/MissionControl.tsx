import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { fetchAnomalies, fetchRollups, fetchTokens, type Anomaly, type Rollups, type TokenTotals, type WorkItem } from "./api";
import { useCountUp } from "./motion";
import { activeStage } from "./taskState";
import LiveAgents from "./LiveAgents";

const NODES = [
  { key: "pfactory", label: "Plan", svc: "PFactory", color: "var(--plan)", cx: 110 },
  { key: "aifactory", label: "Code", svc: "AIFactory", color: "var(--code)", cx: 320 },
  { key: "tfactory", label: "Test", svc: "TFactory", color: "var(--test)", cx: 530 },
] as const;
const CONNECTORS = [
  { from: 0, x1: 162, x2: 268, color: "var(--plan)" },
  { from: 1, x1: 372, x2: 478, color: "var(--code)" },
];

function fmtDur(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

/* Stage glyphs — drawn centred on (0,0); the caller translates to the node.
   All motion is CSS keyed off these classes (see index.css, #53). */
function DocIcon() {
  return (
    <g className="mc-ic">
      <path className="mc-doc-page" d="M-11 -15 H4 L11 -8 V15 H-11 Z" />
      <path className="mc-doc-fold" d="M4 -15 V-8 H11" />
      <line className="mc-doc-l mc-doc-l1" x1={-6} y1={-3} x2={6} y2={-3} />
      <line className="mc-doc-l mc-doc-l2" x1={-6} y1={3} x2={6} y2={3} />
      <line className="mc-doc-l mc-doc-l3" x1={-6} y1={9} x2={2} y2={9} />
    </g>
  );
}

/* Same robot as the Live-agents avatar, so the two views rhyme. */
function BotIcon() {
  return (
    <g className="mc-ic mc-bot mc-bot--live" transform="scale(1.05) translate(-16 -16)">
      <line className="mc-bot-antenna" x1={16} y1={3.5} x2={16} y2={7.5} />
      <circle className="mc-bot-led" cx={16} cy={2.6} r={1.7} />
      <rect className="mc-bot-head" x={5} y={7.5} width={22} height={17.5} rx={5} />
      <rect className="mc-bot-ear" x={2.4} y={13} width={2.6} height={6.5} rx={1.3} />
      <rect className="mc-bot-ear" x={27} y={13} width={2.6} height={6.5} rx={1.3} />
      <circle className="mc-bot-eye" cx={12} cy={15.5} r={2.2} />
      <circle className="mc-bot-eye" cx={20} cy={15.5} r={2.2} />
      <rect className="mc-bot-mouth" x={11} y={20.5} width={10} height={2.4} rx={1.2} />
    </g>
  );
}

function FlaskIcon() {
  return (
    <g className="mc-ic">
      <path className="mc-flask-body" d="M-4 -15 V-3 L-11 12 Q-12 16 -8 16 H8 Q12 16 11 12 L4 -3 V-15" />
      <line className="mc-flask-lip" x1={-6.5} y1={-15} x2={6.5} y2={-15} />
      <path className="mc-flask-liquid" d="M-7.5 5 L-11 12 Q-12 16 -8 16 H8 Q12 16 11 12 L7.5 5 Z" />
      <circle className="mc-bub mc-bub1" cx={-2} cy={12} r={1.5} />
      <circle className="mc-bub mc-bub2" cx={3} cy={13} r={1.2} />
      <circle className="mc-bub mc-bub3" cx={0} cy={14} r={1} />
    </g>
  );
}

const STAGE_ICON: Record<string, () => React.JSX.Element> = {
  pfactory: DocIcon,
  aifactory: BotIcon,
  tfactory: FlaskIcon,
};

function severityIcon(sev: string) {
  if (sev === "high")
    return <path d="M12 3 L22 20 H2 Z M12 9 v5 M12 17 h.01" />;
  if (sev === "medium")
    return <g><circle cx="12" cy="12" r="9" /><path d="M12 7 v6 M12 16 h.01" /></g>;
  return <g><circle cx="12" cy="12" r="9" /><path d="M12 11 v5 M12 8 h.01" /></g>;
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

  // Badges show live work only: each item is counted at the furthest stage that
  // is still active (running / in review / queued). Finished or idle items drop
  // off so the pipeline reflects what's happening now, not lifetime totals.
  const counts = useMemo(() => {
    const c = { pfactory: 0, aifactory: 0, tfactory: 0 };
    for (const wi of items) {
      const stage = activeStage({
        pfactory: wi.pfactory.status,
        aifactory: wi.aifactory.status,
        tfactory: wi.tfactory.status,
      });
      if (stage) c[stage]++;
    }
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
        <svg viewBox="0 0 640 180" className="mc-svg" role="img" aria-label="Pipeline flow: Plan to Code to Test">
          {CONNECTORS.map((c, i) => {
            const flowing = counts[NODES[c.from + 1].key] > 0;
            return (
              <g key={i}>
                <line x1={c.x1} y1={90} x2={c.x2} y2={90} stroke="var(--border)" strokeWidth={2} strokeDasharray="3 6" />
                <line
                  className="mc-flow-dash"
                  x1={c.x1} y1={90} x2={c.x2} y2={90}
                  stroke={c.color}
                  style={{ opacity: flowing ? 0.55 : 0.12 }}
                />
                {flowing && !reduced &&
                  [0, 1.1].map((delay, j) => (
                    <motion.g
                      key={j}
                      initial={{ x: c.x1 }}
                      animate={{ x: [c.x1, c.x2] }}
                      transition={{ duration: 2.2, repeat: Infinity, ease: "easeInOut", delay }}
                    >
                      <g className="mc-pkg" transform="translate(-9 81)">
                        <rect className="mc-pkg-box" width={18} height={18} rx={3} stroke={c.color} strokeWidth={1.6} />
                        <line className="mc-pkg-tape" x1={9} y1={0} x2={9} y2={18} stroke={c.color} />
                        <line className="mc-pkg-tape" x1={0} y1={6} x2={18} y2={6} stroke={c.color} />
                      </g>
                    </motion.g>
                  ))}
              </g>
            );
          })}
          {NODES.map((n) => {
            const active = counts[n.key] > 0;
            const Icon = STAGE_ICON[n.key];
            return (
              <g key={n.key} opacity={active ? 1 : 0.62}>
                {active && !reduced && (
                  <motion.circle
                    cx={n.cx} cy={90} r={46} fill="none" stroke={n.color} strokeWidth={2}
                    initial={{ opacity: 0.5, scale: 1 }}
                    animate={{ opacity: [0.5, 0, 0.5], scale: [1, 1.22, 1] }}
                    transition={{ duration: 2.4, repeat: Infinity, ease: "easeOut" }}
                    style={{ transformOrigin: `${n.cx}px 90px` }}
                  />
                )}
                <circle cx={n.cx} cy={90} r={42} fill="var(--panel)" stroke={n.color} strokeWidth={active ? 2.5 : 1.5} />
                <g transform={`translate(${n.cx} 84)`}>
                  <Icon />
                </g>
                <g transform={`translate(${n.cx + 28} 64)`}>
                  <circle r={11} fill={n.color} />
                  <text className="mc-node-badge" textAnchor="middle" y={4} fill="var(--bg)">{counts[n.key]}</text>
                </g>
                <text x={n.cx} y={150} textAnchor="middle" className="mc-node-lbl" fill={n.color}>{n.label}</text>
                <text x={n.cx} y={166} textAnchor="middle" className="mc-node-svc" fill="var(--faint)">{n.svc}</text>
              </g>
            );
          })}
        </svg>
      </section>

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
                      <span className="anomaly-key">#{a.correlation_key}</span>
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
