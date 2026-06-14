// taskFlow — pure layout + classification for the live execution diagram (#94).
//
// Kept separate from TaskFlow.tsx (the renderer) so the gnarly bits — status
// classification, wave (column) assignment from a dependency list, and node
// placement — are plain functions we can unit-test without a DOM. The renderer
// just consumes `layoutGraph(...)` and animates.

import type { FlowNode, ProcessGraph } from "./api";

// The five visual states a node can be in. Distinct from taskState.ts's
// lifecycle (which has review/queued) because the diagram cares about a
// different cut: is it waiting, working, finished, broken, or wedged.
export type FlowStatus = "pending" | "active" | "done" | "failed" | "stalled";

const STALLED = /(stalled?|stuck|wedged|stale|timed?[_-]?out|timeout)/i;
const FAILED = /(fail|reject|error|abort|cancel|block|discard)/i;
const DONE =
  /(^|[_-])(done|complete|completed|passed|merged|approved|succeeded|success|shipped|emitted|closed|accepted)([_-]|$)/i;
const ACTIVE = /(in[_-]?progress|running|executing|working|active|coding|planning|generating|building)/i;

/**
 * Classify a node into one of the five diagram states. Order matters: stalled
 * and failed win over everything, then done, then active. A node with a
 * started_at but no terminal status is treated as active (it's been picked up);
 * a bare node with nothing is pending.
 */
export function flowStatus(node: FlowNode): FlowStatus {
  const s = node.status ?? "";
  if (STALLED.test(s)) return "stalled";
  if (FAILED.test(s)) return "failed";
  if (DONE.test(s)) return "done";
  if (ACTIVE.test(s)) return "active";
  // No explicit signal: infer from timestamps. Started-but-not-finished = active.
  if (node.started_at && !node.completed_at) return "active";
  return "pending";
}

/**
 * Assign every node a wave (0-based column) from its dependency edges:
 * wave(n) = 0 when it has no deps, else 1 + max(wave(dep)). A producer-supplied
 * `wave` is honoured as-is. Robust to missing dep ids (ignored), self-loops, and
 * cycles (a visited-guard caps recursion so a bad graph still lays out).
 */
export function assignWaves(nodes: readonly FlowNode[]): Map<string, number> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const wave = new Map<string, number>();

  const resolve = (id: string, stack: Set<string>): number => {
    const cached = wave.get(id);
    if (cached != null) return cached;
    const node = byId.get(id);
    if (!node) return 0;
    if (typeof node.wave === "number" && node.wave >= 0) {
      wave.set(id, node.wave);
      return node.wave;
    }
    const deps = (node.deps ?? []).filter((d) => d !== id && byId.has(d));
    if (deps.length === 0 || stack.has(id)) {
      wave.set(id, 0);
      return 0;
    }
    stack.add(id);
    let w = 0;
    for (const d of deps) w = Math.max(w, resolve(d, stack) + 1);
    stack.delete(id);
    wave.set(id, w);
    return w;
  };

  for (const n of nodes) resolve(n.id, new Set());
  return wave;
}

// --- Layout geometry -----------------------------------------------------

export const NODE_W = 158;
export const NODE_H = 52;
export const COL_GAP = 64; // horizontal space between wave columns
export const ROW_GAP = 18; // vertical space between stacked nodes
export const PAD = 14; // canvas padding

export interface PlacedNode {
  node: FlowNode;
  status: FlowStatus;
  x: number;
  y: number;
  wave: number;
}

export interface FlowEdge {
  from: string;
  to: string;
  // "next" = the edge the work is flowing along right now: source done, target
  // not yet done (and not failed). These animate; the rest are static.
  next: boolean;
  // an edge feeding a failed/stalled node is tinted to match.
  status: FlowStatus;
}

export interface FlowLayout {
  placed: PlacedNode[];
  edges: FlowEdge[];
  width: number;
  height: number;
}

/**
 * Full layout pass: classify, assign waves, place each wave as a column (nodes
 * stacked vertically + vertically centred so short columns sit mid-canvas), and
 * build the edge list with the "next"/status flags the renderer animates on.
 */
export function layoutGraph(graph: ProcessGraph): FlowLayout {
  const nodes = graph.nodes ?? [];
  const waves = assignWaves(nodes);
  const statusById = new Map(nodes.map((n) => [n.id, flowStatus(n)]));

  // Group node ids by wave, preserving input order within a column.
  const cols = new Map<number, FlowNode[]>();
  let maxWave = 0;
  for (const n of nodes) {
    const w = waves.get(n.id) ?? 0;
    maxWave = Math.max(maxWave, w);
    (cols.get(w) ?? cols.set(w, []).get(w)!).push(n);
  }

  const tallest = Math.max(1, ...[...cols.values()].map((c) => c.length));
  const colH = tallest * NODE_H + (tallest - 1) * ROW_GAP;

  const placed: PlacedNode[] = [];
  for (let w = 0; w <= maxWave; w++) {
    const col = cols.get(w) ?? [];
    const thisH = col.length * NODE_H + Math.max(0, col.length - 1) * ROW_GAP;
    const y0 = PAD + (colH - thisH) / 2; // vertically centre this column
    col.forEach((node, i) => {
      placed.push({
        node,
        status: statusById.get(node.id) ?? "pending",
        x: PAD + w * (NODE_W + COL_GAP),
        y: y0 + i * (NODE_H + ROW_GAP),
        wave: w,
      });
    });
  }

  const edges: FlowEdge[] = [];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  for (const n of nodes) {
    for (const dep of n.deps ?? []) {
      if (!byId.has(dep) || dep === n.id) continue;
      const from = statusById.get(dep) ?? "pending";
      const to = statusById.get(n.id) ?? "pending";
      const next = from === "done" && to !== "done" && to !== "failed";
      const status: FlowStatus =
        to === "failed" || to === "stalled" ? to : next ? "active" : from;
      edges.push({ from: dep, to: n.id, next, status });
    }
  }

  return {
    placed,
    edges,
    width: PAD * 2 + (maxWave + 1) * NODE_W + maxWave * COL_GAP,
    height: PAD * 2 + colH,
  };
}

// --- Timer formatting ----------------------------------------------------

/** Elapsed seconds for a node: started→completed for a finished node, or
 *  started→now for one still running. null when we don't have a start. */
export function nodeElapsedSeconds(
  node: FlowNode,
  status: FlowStatus,
  nowMs: number,
): number | null {
  if (!node.started_at) return null;
  const start = Date.parse(node.started_at);
  if (Number.isNaN(start)) return null;
  const endIso = node.completed_at;
  const end =
    status === "active" || status === "stalled" || !endIso
      ? nowMs
      : Date.parse(endIso);
  if (Number.isNaN(end)) return null;
  return Math.max(0, Math.round((end - start) / 1000));
}

/** mm:ss (or h:mm:ss past an hour) for the per-node timer chip. */
export function fmtClock(sec: number | null): string {
  if (sec == null) return "";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
