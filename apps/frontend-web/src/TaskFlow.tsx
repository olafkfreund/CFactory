// TaskFlow — the live execution diagram (#94).
//
// Renders a stage's work (plan children / code subtasks / test lanes) as an
// animated dependency DAG, laid out as wave-columns (taskFlow.ts does the
// geometry). Nodes light up live: green + a robot stamp when done, a pulse while
// active, a red shake on failure, an amber pulse when stalled. The edges between
// them flow with marching dashes along whichever path the work is travelling
// next, so it's obvious at a glance where the pipeline is and what's blocked.
//
// House style: hand-rolled SVG for the edges (no graph lib — matches the
// cockpit's no-dependency charting in LiveTaskStamp), HTML node cards over the
// top for crisp text + live timers, framer-motion for the state transitions, and
// the gruvbox stage palette (--plan/--code/--test) for the accents. Additive:
// given an empty graph it renders nothing, so the modal looks exactly as before.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import type { ProcessGraph } from "./api";
import {
  layoutGraph,
  flowStatus,
  nodeElapsedSeconds,
  fmtClock,
  NODE_W,
  NODE_H,
  type FlowStatus,
  type PlacedNode,
  type FlowEdge,
} from "./taskFlow";
import { useNow } from "./motion";
import { IconRobot } from "./icons";

const STAGE_ACCENT: Record<ProcessGraph["stage"], string> = {
  plan: "var(--plan)",
  code: "var(--code)",
  test: "var(--test)",
};

const STAGE_LABEL: Record<ProcessGraph["stage"], string> = {
  plan: "Plan",
  code: "Code",
  test: "Test",
};

// A cubic bezier from the right edge of the source card to the left edge of the
// target card — a gentle S-curve that reads cleanly even when columns stack.
function edgePath(a: PlacedNode, b: PlacedNode): string {
  const x1 = a.x + NODE_W;
  const y1 = a.y + NODE_H / 2;
  const x2 = b.x;
  const y2 = b.y + NODE_H / 2;
  const dx = Math.max(28, (x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

function edgeColor(s: FlowStatus): string {
  if (s === "failed") return "var(--red)";
  if (s === "stalled") return "var(--amber)";
  if (s === "done") return "var(--green)";
  if (s === "active") return "var(--cyan)";
  return "var(--border)";
}

function Edges({ edges, placedById }: { edges: FlowEdge[]; placedById: Map<string, PlacedNode> }) {
  return (
    <g>
      {edges.map((e, i) => {
        const a = placedById.get(e.from);
        const b = placedById.get(e.to);
        if (!a || !b) return null;
        const color = edgeColor(e.status);
        return (
          <path
            key={`${e.from}->${e.to}-${i}`}
            className={`tf-edge${e.next ? " tf-edge--flow" : ""}`}
            d={edgePath(a, b)}
            style={{ stroke: color, color }}
            fill="none"
          />
        );
      })}
    </g>
  );
}

const SHAKE = { x: [0, -3, 3, -2, 2, 0] };

function Node({ p, nowMs, reduced }: { p: PlacedNode; nowMs: number; reduced: boolean }) {
  const { node, status } = p;
  const elapsed = nodeElapsedSeconds(node, status, nowMs);
  const clock = fmtClock(elapsed);

  // Per-state motion. Done pops once; failed shakes once; stalled/active breathe
  // via CSS (cheaper than JS for an indefinite loop). Reduced-motion users get
  // the colour + icon, no movement.
  const animate =
    reduced
      ? {}
      : status === "failed"
        ? SHAKE
        : status === "done"
          ? { scale: [0.96, 1.03, 1] }
          : {};

  return (
    <motion.div
      className={`tf-node tf-node--${status}`}
      style={{ left: p.x, top: p.y, width: NODE_W, height: NODE_H }}
      initial={reduced ? false : { opacity: 0, scale: 0.9 }}
      animate={{ opacity: 1, scale: 1, ...animate }}
      transition={{ duration: 0.4 }}
      title={`${node.label}${node.kind ? ` · ${node.kind}` : ""} — ${status}${clock ? ` · ${clock}` : ""}`}
    >
      <span className="tf-node-dot" />
      <span className="tf-node-label">{node.label}</span>
      <span className="tf-node-meta">
        {clock && <span className="tf-node-time">{clock}</span>}
        <AnimatePresence>
          {status === "done" && (
            <motion.span
              className="tf-node-bot"
              initial={reduced ? false : { scale: 0, rotate: -20, opacity: 0 }}
              animate={{ scale: 1, rotate: 0, opacity: 1 }}
              transition={{ type: "spring", stiffness: 420, damping: 16 }}
              aria-hidden="true"
            >
              <IconRobot size={14} />
            </motion.span>
          )}
        </AnimatePresence>
      </span>
    </motion.div>
  );
}

function Legend({ counts }: { counts: Record<FlowStatus, number> }) {
  const items: Array<[FlowStatus, string]> = [
    ["done", "done"],
    ["active", "running"],
    ["pending", "waiting"],
    ["failed", "failed"],
    ["stalled", "stalled"],
  ];
  return (
    <div className="tf-legend">
      {items.map(([k, label]) =>
        counts[k] > 0 ? (
          <span key={k} className={`tf-leg tf-leg--${k}`}>
            <span className="tf-leg-dot" />
            {counts[k]} {label}
          </span>
        ) : null,
      )}
    </div>
  );
}

// Stage switcher (#94 follow-up): a task carries a graph per stage (plan / code /
// test). The modal shows the furthest stage by default but lets you flip to any
// available stage — so the plan DAG stays viewable after coding begins.
const SWITCH_ORDER: Array<"plan" | "code" | "test"> = ["plan", "code", "test"];

export function StageFlow({
  graphs,
  fallback,
}: {
  graphs?: Partial<Record<"plan" | "code" | "test", ProcessGraph>>;
  fallback?: ProcessGraph | null;
}) {
  const available = SWITCH_ORDER.filter((s) => graphs?.[s]);
  const furthest = available.length ? available[available.length - 1] : null;
  const [sel, setSel] = useState<"plan" | "code" | "test" | null>(null);

  // Default to the furthest stage; keep the selection valid as data streams in.
  useEffect(() => {
    setSel((cur) => (cur && graphs?.[cur] ? cur : furthest));
  }, [furthest, graphs]);

  // No multi-stage map → render the single furthest graph (back-compat).
  if (!available.length) return fallback ? <TaskFlow graph={fallback} /> : null;

  const cur = sel && graphs?.[sel] ? sel : furthest!;
  return (
    <div className="tf-stack">
      {available.length > 1 && (
        <div className="tf-switch" role="tablist" aria-label="Stage">
          {available.map((s) => (
            <button
              key={s}
              role="tab"
              aria-selected={cur === s}
              className={`tf-switch-tab tf-switch-tab--${s}${cur === s ? " on" : ""}`}
              onClick={() => setSel(s)}
            >
              {STAGE_LABEL[s]}
            </button>
          ))}
        </div>
      )}
      <TaskFlow graph={graphs![cur]} />
    </div>
  );
}

export default function TaskFlow({ graph }: { graph: ProcessGraph | null | undefined }) {
  const reduced = useReducedMotion() ?? false;

  const layout = useMemo(() => (graph && graph.nodes?.length ? layoutGraph(graph) : null), [graph]);

  // Tick once a second only while something is live (active or stalled), so the
  // running-node timers advance but a finished diagram is perfectly still.
  const anyLive = useMemo(
    () => !!graph?.nodes?.some((n) => { const s = flowStatus(n); return s === "active" || s === "stalled"; }),
    [graph],
  );
  const nowMs = useNow(anyLive);

  const placedById = useMemo(
    () => new Map((layout?.placed ?? []).map((p) => [p.node.id, p])),
    [layout],
  );

  const counts = useMemo(() => {
    const c: Record<FlowStatus, number> = { pending: 0, active: 0, done: 0, failed: 0, stalled: 0 };
    for (const p of layout?.placed ?? []) c[p.status] += 1;
    return c;
  }, [layout]);

  // --- Zoom / pan / fit (#94 follow-up) ---------------------------------
  // The overflow container handles pan (scroll); zoom is a scale() on the
  // content, with a sizer reserving the scaled space so scrollbars track. Large
  // plans become navigable instead of overflowing.
  const canvasRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(1);
  const [userZoomed, setUserZoomed] = useState(false);
  const Z_MIN = 0.3;
  const Z_MAX = 1.5;
  const clamp = (z: number) => Math.min(Z_MAX, Math.max(Z_MIN, z));

  const w = layout?.width ?? 0;
  const h = layout?.height ?? 0;

  // Fit-to-view: scale so the whole graph fits the container width (never up-
  // scaling past 1). Runs on graph change until the user takes manual control.
  const fit = useCallback(() => {
    const el = canvasRef.current;
    if (!el || w === 0) return 1;
    const avail = el.clientWidth - 8;
    return clamp(Math.min(1, avail / w));
  }, [w]);

  useLayoutEffect(() => {
    if (userZoomed) return;
    setZoom(fit());
  }, [fit, userZoomed, w, h]);

  // Re-fit on container resize (until the user zooms manually).
  useEffect(() => {
    const el = canvasRef.current;
    if (!el || userZoomed || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setZoom(fit()));
    ro.observe(el);
    return () => ro.disconnect();
  }, [fit, userZoomed]);

  const zoomBy = useCallback((delta: number) => {
    setUserZoomed(true);
    setZoom((z) => clamp(Math.round((z + delta) * 100) / 100));
  }, []);
  const resetZoom = useCallback(() => {
    setUserZoomed(false);
    setZoom(fit());
  }, [fit]);

  // Scroll the first active (or stalled) node into view — "where are we now".
  const focusActive = useCallback(() => {
    const el = canvasRef.current;
    if (!el || !layout) return;
    const live = layout.placed.find((p) => p.status === "active" || p.status === "stalled");
    if (!live) return;
    el.scrollTo({ left: Math.max(0, live.x * zoom - el.clientWidth / 3), behavior: "smooth" });
  }, [layout, zoom]);

  if (!graph || !layout || layout.placed.length === 0) return null;

  const accent = STAGE_ACCENT[graph.stage];
  const total = layout.placed.length;
  const done = counts.done;
  const hasLive = counts.active > 0 || counts.stalled > 0;

  return (
    <section className="td-section tf">
      <div className="tf-head">
        <h3>
          <span className="tf-stage-dot" style={{ background: accent }} />
          {STAGE_LABEL[graph.stage]} flow
          <span className="tf-progress">
            {done}/{total}
          </span>
        </h3>
        <Legend counts={counts} />
      </div>

      <div
        className="tf-canvas"
        ref={canvasRef}
        role="img"
        aria-label={`${graph.stage} execution diagram, ${done} of ${total} done`}
      >
        {/* Zoom + navigation controls. Shown for any non-trivial graph. */}
        {total > 1 && (
          <div className="tf-zoom" role="group" aria-label="Diagram zoom">
            {hasLive && (
              <button className="tf-zoom-btn tf-zoom-focus" onClick={focusActive} title="Scroll to the active node">
                active
              </button>
            )}
            <button className="tf-zoom-btn" onClick={() => zoomBy(-0.15)} aria-label="Zoom out" title="Zoom out">
              &minus;
            </button>
            <span className="tf-zoom-pct" title="Zoom level">{Math.round(zoom * 100)}%</span>
            <button className="tf-zoom-btn" onClick={() => zoomBy(0.15)} aria-label="Zoom in" title="Zoom in">
              +
            </button>
            <button className="tf-zoom-btn tf-zoom-fit" onClick={resetZoom} title="Fit to view">
              fit
            </button>
          </div>
        )}

        {/* Sizer reserves the scaled space so the overflow container can pan. */}
        <div className="tf-zoom-sizer" style={{ width: w * zoom, height: h * zoom }}>
          <div
            className="tf-stage-tint"
            style={{
              ["--tf-accent" as string]: accent,
              width: w,
              height: h,
              transform: `scale(${zoom})`,
              transformOrigin: "top left",
            }}
          >
            <svg className="tf-edges" width={w} height={h} aria-hidden="true">
              <Edges edges={layout.edges} placedById={placedById} />
            </svg>
            <div className="tf-nodes" style={{ width: w, height: h }}>
              {layout.placed.map((p) => (
                <Node key={p.node.id} p={p} nowMs={nowMs} reduced={reduced} />
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
