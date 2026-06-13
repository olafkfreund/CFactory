// LiveTaskStamp — Tier 1 of the per-worker observability work
// (design: Factory/docs/plans/2026-06-13-per-worker-observability-design.md).
//
// A compact "stamp" for a running task card: accumulated cost_usd, total
// tokens, # workers done, and elapsed — plus a tiny inline SVG sparkline of
// CUMULATIVE COST across the workers in completion order (it grows one step per
// finished worker). This is the "ticks on each worker completion" graph; a
// future per-second time-series (from heartbeat events) is left as a seam below.
//
// Everything here is ADDITIVE and GUARDED: given an empty/absent worker set the
// component renders nothing, so a running card with no per-worker data yet looks
// exactly as it did before this feature shipped.

import { useMemo } from "react";
import type { WorkerRow, WorkItem } from "./api";

// --- Pure, unit-testable derivation --------------------------------------

export interface StampStats {
  costUsd: number;
  totalTokens: number;
  workersDone: number;
  // Cumulative cost after each worker, in completion order. Length === #workers.
  // Drives the sparkline; empty when there are no workers.
  cumulativeCost: number[];
}

// A worker is "done" once it has reported usage. The by_worker rollup only ever
// lists workers that have finished (it is keyed off completion records), so
// every row here counts — but we stay defensive in case that ever changes.
function workerIsDone(w: WorkerRow): boolean {
  return (w.total_tokens ?? 0) > 0 || (w.cost_usd ?? 0) > 0 || (w.duration_ms ?? 0) > 0;
}

/**
 * Fold a task's per-worker rows into the stamp's headline numbers + the
 * cumulative-cost series. `workers` is taken in completion order as delivered
 * by /api/tokens/by_worker (append-on-finish); we do not re-sort so the
 * sparkline reflects the real order workers landed in.
 */
export function deriveStampStats(workers: readonly WorkerRow[]): StampStats {
  let costUsd = 0;
  let totalTokens = 0;
  let workersDone = 0;
  const cumulativeCost: number[] = [];
  for (const w of workers) {
    costUsd += w.cost_usd ?? 0;
    totalTokens += w.total_tokens ?? 0;
    if (workerIsDone(w)) workersDone += 1;
    cumulativeCost.push(costUsd);
  }
  return { costUsd, totalTokens, workersDone, cumulativeCost };
}

// Elapsed is derived from the WorkItem's own timeline: first event = start,
// last event = most recent update. No new backend field is needed. Returns a
// seconds count, or null when there isn't enough timeline to be honest.
export function deriveElapsedSeconds(wi: Pick<WorkItem, "timeline">, nowMs = Date.now()): number | null {
  const tl = wi.timeline;
  if (!tl || tl.length === 0) return null;
  let firstMs = Infinity;
  let lastMs = -Infinity;
  for (const e of tl) {
    const t = Date.parse(e.updated_at);
    if (Number.isNaN(t)) continue;
    if (t < firstMs) firstMs = t;
    if (t > lastMs) lastMs = t;
  }
  if (!Number.isFinite(firstMs)) return null;
  // For a running task the work is still in flight, so measure to "now" rather
  // than to the last recorded event (which can lag the live cost we're showing).
  const end = Math.max(lastMs, nowMs);
  return Math.max(0, Math.round((end - firstMs) / 1000));
}

// --- Tiny formatters (mirror TokensView's house style) -------------------

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(Math.round(n));
}

function fmtCost(n: number): string {
  // Sub-cent costs (common for a single Ollama/short worker) shouldn't read $0.00.
  if (n > 0 && n < 0.01) return "<$0.01";
  return `$${n.toFixed(2)}`;
}

function fmtElapsed(sec: number | null): string {
  if (sec == null) return "—";
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return `${h}h ${m}m`;
}

// --- Sparkline (hand-rolled SVG, no chart lib) ---------------------------
//
// SEAM: this draws cumulative cost stepped by WORKER COMPLETION. A future
// per-second time-series (driven by per-worker heartbeat events — design P2)
// would feed a denser series in here; the polyline math below is series-shape
// agnostic, so that upgrade is a data swap, not a rewrite.

function CostSparkline({ series, width = 64, height = 18 }: { series: number[]; width?: number; height?: number }) {
  // Need at least two points to draw a line; a single worker shows just a dot.
  const max = Math.max(...series, 0);
  const n = series.length;
  const pad = 2;
  const innerW = width - pad * 2;
  const innerH = height - pad * 2;

  const x = (i: number) => (n <= 1 ? width / 2 : pad + (i / (n - 1)) * innerW);
  // Cost only grows, so a flat-zero series (all-Ollama, cost 0) pins to the
  // baseline rather than dividing by zero.
  const y = (v: number) => (max <= 0 ? height - pad : pad + innerH - (v / max) * innerH);

  const points = series.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const lastX = x(n - 1);
  const lastY = y(series[n - 1] ?? 0);

  return (
    <svg
      className="lts-spark"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      aria-hidden="true"
      preserveAspectRatio="none"
    >
      {n > 1 && <polyline className="lts-spark-line" points={points} fill="none" />}
      <circle className="lts-spark-dot" cx={lastX} cy={lastY} r={1.8} />
    </svg>
  );
}

// --- Component ------------------------------------------------------------

export default function LiveTaskStamp({
  wi,
  workers,
}: {
  wi: Pick<WorkItem, "timeline">;
  // The per-worker rows for THIS task (from /api/tokens/by_worker), or undefined
  // when the by_worker fetch hasn't landed / failed. Either way we no-op.
  workers: readonly WorkerRow[] | undefined;
}) {
  const stats = useMemo(() => (workers && workers.length ? deriveStampStats(workers) : null), [workers]);
  const elapsed = useMemo(() => deriveElapsedSeconds(wi), [wi]);

  // Guard: no worker data → render exactly as before (nothing).
  if (!stats) return null;

  return (
    <div className="lts" title="Live per-worker cost & usage (Tier 1)">
      <CostSparkline series={stats.cumulativeCost} />
      <span className="lts-metric lts-cost" title={`$${stats.costUsd.toFixed(4)} so far`}>
        {fmtCost(stats.costUsd)}
      </span>
      <span className="lts-metric lts-tok" title={`${stats.totalTokens.toLocaleString()} tokens`}>
        {fmtTokens(stats.totalTokens)} tok
      </span>
      <span className="lts-metric lts-workers" title={`${stats.workersDone} worker(s) done`}>
        {stats.workersDone}w
      </span>
      <span className="lts-metric lts-elapsed" title="Elapsed since first event">
        {fmtElapsed(elapsed)}
      </span>
    </div>
  );
}
