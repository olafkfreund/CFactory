import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { fetchAnomalies, fetchRollups, fetchTokens, type Anomaly, type BillingSummary, type Rollups, type TokenTotals, type WorkItem, type WorkItemTokenRow } from "./api";
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
        <FleetUsageStat tokens={tokens} />
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

        <CostByTask tokens={tokens} />

        <LiveAgents reloadSignal={reloadSignal} />
      </div>
    </>
  );
}

function fmtTokens(n: number): string {
  if (!n) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 100_000 ? 0 : 1)}k`;
  return `${n}`;
}

function fmtCost(c: number): string {
  return c >= 1 ? `$${c.toFixed(2)}` : `$${c.toFixed(3)}`;
}

function fmtElapsed(sec?: number): string {
  if (sec == null) return "";
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return `${h}h ${m}m`;
}

// Billing mode → a small, non-scary tag. Metered modes (api/cloud) are accented
// in the code/yellow; subscription is green ("included"); local is cyan.
const MODE_TONE: Record<string, string> = {
  api: "var(--code)", cloud: "var(--code)", subscription: "var(--green)",
  local: "var(--cyan)", unknown: "var(--faint)",
};
const MODE_LABEL: Record<string, string> = {
  api: "API", cloud: "cloud", subscription: "subscription", local: "local", unknown: "usage",
};
function billingTag(b?: BillingSummary): { label: string; tone: string } | null {
  if (!b || b.modes.length === 0) return null;
  if (b.modes.length > 1) return { label: "mixed", tone: "var(--violet)" };
  const m = b.modes[0];
  if (m === undefined) return null; // length is exactly 1 here, but prove it
  return { label: MODE_LABEL[m] ?? m, tone: MODE_TONE[m] ?? "var(--faint)" };
}

// Fleet headline: real dollars only when something was actually metered;
// otherwise tokens (a subscription/local run spent no money to show).
function FleetUsageStat({ tokens }: { tokens: TokenTotals | null }) {
  const metered = tokens?.total.metered_cost_usd ?? tokens?.total.cost_usd ?? 0;
  const hasModes = tokens?.total.has_billing_modes ?? false;
  if (metered > 0) {
    return <StatText label="Spend (USD)" text={`$${metered.toFixed(2)}`} accent="var(--code)" />;
  }
  const tok = tokens?.total.total_tokens ?? 0;
  return (
    <StatText
      label="Tokens"
      text={tok > 0 ? fmtTokens(tok) : "—"}
      sub={tok > 0 ? (hasModes ? "subscription / local" : undefined) : "awaiting usage"}
      accent="var(--code)"
    />
  );
}

// Per-task usage — sourced from CFactory's own event store (/api/tokens). Shows
// the RIGHT metric per billing mode (#96): real dollars only for metered
// (api/cloud) work, tokens + time for subscription/local — never a notional $.
// Ranked by tokens (universal), with time spent and a soft-budget badge.
function CostByTask({ tokens }: { tokens: TokenTotals | null }) {
  const rows: WorkItemTokenRow[] = (tokens?.by_work_item ?? [])
    .filter((t) => t.total_tokens > 0 || t.cost_usd > 0)
    .sort((a, b) => b.total_tokens - a.total_tokens);
  const top = rows.slice(0, 6);
  const maxTok = top.reduce((m, t) => Math.max(m, t.total_tokens), 0);
  const fleetMetered = tokens?.total.metered_cost_usd ?? 0;

  return (
    <div className="mc-panel cost-panel">
      <div className="panel-title-row">
        <h2 className="panel-title">Usage by task</h2>
        {fleetMetered > 0 ? (
          <span className="cost-grand">{fmtCost(fleetMetered)}</span>
        ) : tokens && tokens.total.total_tokens > 0 ? (
          <span className="cost-grand cost-grand--tok">{fmtTokens(tokens.total.total_tokens)} tok</span>
        ) : null}
      </div>

      {top.length === 0 ? (
        <div className="cost-empty">No task usage yet — runs populate this live.</div>
      ) : (
        <div className="cost-list">
          <AnimatePresence initial={false}>
            {top.map((t) => {
              const pct = maxTok > 0 ? Math.max(4, (t.total_tokens / maxTok) * 100) : 0;
              const tag = billingTag(t.billing);
              const metered = t.billing?.has_metered ?? false;
              // Headline: real cost only when metered; else tokens.
              const meteredCost = t.billing?.metered_cost_usd ?? t.cost_usd;
              const over = metered ? t.budget?.exceeded : false;
              const time = fmtElapsed(t.elapsed_seconds);
              return (
                <motion.div
                  key={t.correlation_key}
                  className={`cost-row${over ? " cost-row--over" : ""}`}
                  initial={{ opacity: 0, x: 10 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.25 }}
                >
                  <div className="cost-row-top">
                    <span className="cost-key">#{keySlug(t.correlation_key)}</span>
                    <span className="cost-title" title={t.title ?? t.correlation_key}>
                      {t.title ?? "untitled"}
                    </span>
                    {tag && (
                      <span className="cost-mode" style={{ color: tag.tone, borderColor: tag.tone }}>
                        {tag.label}
                      </span>
                    )}
                    {over && <span className="cost-budge">over budget</span>}
                    {metered && meteredCost > 0 ? (
                      <span className="cost-amt">{fmtCost(meteredCost)}</span>
                    ) : (
                      <span className="cost-amt cost-amt--tok">{fmtTokens(t.total_tokens)}</span>
                    )}
                  </div>
                  <div className="cost-meter">
                    <motion.span
                      className="cost-fill"
                      initial={{ width: 0 }}
                      animate={{ width: `${pct}%` }}
                      transition={{ duration: 0.5, ease: "easeOut" }}
                    />
                  </div>
                  <div className="cost-sub">
                    <span>{fmtTokens(t.total_tokens)} tokens</span>
                    {time && <span className="cost-sub-time">· {time}</span>}
                    {metered && t.budget && (
                      <span className="cost-sub-budget">
                        · {fmtCost(t.budget.spent_usd)} / {fmtCost(t.budget.limit_usd)}
                      </span>
                    )}
                  </div>
                </motion.div>
              );
            })}
          </AnimatePresence>
        </div>
      )}
    </div>
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

function StatText({ label, text, sub, accent }: { label: string; text: string; sub?: string | undefined; accent: string }) {
  return (
    <div className="mc-stat">
      <div className="mc-stat-v" style={{ color: accent }}>{text}</div>
      <div className="mc-stat-l">{label}{sub && <span className="mc-stat-sub"> · {sub}</span>}</div>
    </div>
  );
}
