// The Pipeline board view (plan → code → test columns), its work-item cards and
// the live/stage chips — extracted from App.tsx (#115). Pure extraction —
// behaviour-preserving, no logic or UI change.
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { AnimatePresence } from "framer-motion";
import { refresh as apiRefresh, type LiveProgress, type WorkItem } from "./api";
import TaskDetail from "./TaskDetail";
import { hasFailedStage, stageFailed, type StripStage } from "./pipelineStages";
import { activeStage, STATE_LABEL, stageState } from "./taskState";
import { displayTitle, keySlug } from "./correlationKey";
import { WorkCard, type WorkPhase, type WorkTone } from "./components/factory-ui/WorkCard";
import { WorkColumn } from "./components/factory-ui/WorkColumn";
import { itemState, relTime, STAGES, type StageKey } from "./dashboard";

export type BoardChip = "all" | "running" | "review" | "queued" | "failed";

const BOARD_CHIPS: { id: BoardChip; label: string }[] = [
  { id: "all", label: "All" },
  { id: "running", label: "Running" },
  { id: "review", label: "In review" },
  { id: "queued", label: "Queued" },
  { id: "failed", label: "Failed" },
];

export function Board({
  items,
  progress,
  stageFilter,
  onClearStage,
  initialChip = "all",
  chipSignal = 0,
  onChipConsumed,
  onChipChange,
  projectId,
}: {
  items: WorkItem[];
  progress: Record<string, LiveProgress>;
  stageFilter: StripStage | null;
  onClearStage: () => void;
  initialChip?: BoardChip | undefined;
  chipSignal?: number | undefined;
  onChipConsumed?: (() => void) | undefined;
  onChipChange?: ((chip: BoardChip) => void) | undefined;
  projectId?: string | undefined;
}) {
  const [query, setQuery] = useState("");
  const [chip, setChip] = useState<BoardChip>(initialChip);
  const consumedSignal = useRef(0);
  const [showFinished, setShowFinished] = useState(false);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  useEffect(() => {
    if (!chipSignal || consumedSignal.current === chipSignal) return;
    consumedSignal.current = chipSignal;
    setChip(initialChip);
    onChipConsumed?.();
  }, [initialChip, chipSignal, onChipConsumed]);

  // Same lifecycle semantics as the Mission Control ring and the pipeline
  // strip: an item lives at its furthest ACTIVE stage; finished/idle items
  // drop into the (hidden by default) finished section instead of inflating
  // the columns. When the Failed chip is active, failed tasks in the Finished
  // section or in another active column still show under the stage that failed.
  const { byStage, finished } = useMemo(() => {
    const m: Record<StageKey, WorkItem[]> = { pfactory: [], aifactory: [], tfactory: [] };
    const finished: WorkItem[] = [];
    for (const wi of items) {
      if (chip === "failed") {
        let placed = false;
        for (const k of ["pfactory", "aifactory", "tfactory"] as const) {
          if (stageFailed(wi, k)) {
            m[k].push(wi);
            placed = true;
          }
        }
        if (placed) continue;
      }
      const stage = activeStage({
        pfactory: wi.pfactory.status,
        aifactory: wi.aifactory.status,
        tfactory: wi.tfactory.status,
      });
      if (stage) m[stage].push(wi);
      else finished.push(wi);
    }
    return { byStage: m, finished };
  }, [items, chip]);

  const q = query.trim().toLowerCase();
  const matchesQuery = useCallback(
    (wi: WorkItem) =>
      !q ||
      wi.correlation_key.toLowerCase().includes(q) ||
      (wi.title ?? "").toLowerCase().includes(q) ||
      displayTitle(wi.title, wi.correlation_key).toLowerCase().includes(q),
    [q],
  );

  const visible = useCallback(
    (wi: WorkItem) =>
      matchesQuery(wi) &&
      (chip === "all" ||
        (chip === "failed" ? hasFailedStage(wi, stageFilter) : itemState(wi) === chip)),
    [matchesQuery, chip, stageFilter],
  );

  const chipCounts = useMemo(() => {
    const active = items
      .filter((wi) =>
        Boolean(
          activeStage({
            pfactory: wi.pfactory.status,
            aifactory: wi.aifactory.status,
            tfactory: wi.tfactory.status,
          }),
        ),
      )
      .filter(matchesQuery);
    const c: Record<BoardChip, number> = {
      all: active.length,
      running: 0,
      review: 0,
      queued: 0,
      failed: items.filter((wi) => matchesQuery(wi) && hasFailedStage(wi, stageFilter)).length,
    };
    for (const wi of active) {
      const st = itemState(wi);
      if (st === "running" || st === "review" || st === "queued") c[st]++;
    }
    return c;
  }, [items, matchesQuery, stageFilter]);

  const stages = STAGES.filter((s) => !stageFilter || s.key === stageFilter);
  const shownFinished = finished.filter(visible);

  // Resolve against the latest items so the open drawer updates live.
  const selected = selectedKey
    ? (items.find((w) => w.correlation_key === selectedKey) ?? null)
    : null;

  return (
    <>
      <div className="page-head">
        <h1>Pipeline</h1>
        <p>
          Every work item at its current active stage — plan → code → test by GitHub issue. Click a
          card for live detail.
        </p>
      </div>

      <div className="rt-toolbar board-toolbar">
        <input
          className="board-search"
          type="search"
          placeholder="Filter by title or key…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Filter work items"
        />
        {BOARD_CHIPS.map((c) => (
          <button
            key={c.id}
            className={`rt-chip ${chip === c.id ? "active" : ""} rt-chip--${c.id}`}
            onClick={() => { setChip(c.id); onChipChange?.(c.id); }}
          >
            {c.label} <span className="rt-chip-n">{chipCounts[c.id]}</span>
          </button>
        ))}
        <span className="board-toolbar-spacer" />
        {stageFilter && (
          <button
            className="rt-chip active"
            onClick={onClearStage}
            title="Showing one pipeline stage — click to clear"
          >
            stage: {STAGES.find((s) => s.key === stageFilter)?.label} ✕
          </button>
        )}
        <button
          className={`rt-chip ${showFinished ? "active" : ""}`}
          onClick={() => setShowFinished((v) => !v)}
          aria-pressed={showFinished}
        >
          Finished <span className="rt-chip-n">{shownFinished.length}</span>
        </button>
      </div>

      <section
        className="board"
        aria-label="Pipeline board"
        style={{ "--board-cols": stages.length } as CSSProperties}
      >
        {stages.map((stage) => {
          const cards = byStage[stage.key].filter(visible);
          return (
            <WorkColumn key={stage.key} title={`${stage.label} · ${stage.svc}`} tone={stage.cls === "code" ? "build" : stage.cls} projectId={projectId}
              emptyLabel={byStage[stage.key].length === 0 ? "No active work items" : "Nothing matches the filter"}
              showMoreLabel={(count) => `Show ${String(count)} more`}
              items={cards.map((wi) => ({ id: wi.correlation_key, projectId: projectId ?? wi.pfactory.repo ?? wi.aifactory.repo ?? wi.tfactory.repo ?? "fleet", collapsedByDefault: itemState(wi) === "done",
                render: (state) => <Card wi={wi} lp={progress[wi.correlation_key]} expanded={state.expanded} onToggle={() => { state.onToggle(); }} onOpen={() => setSelectedKey(wi.correlation_key)} /> }))} />
          );
        })}
      </section>

      {showFinished && (
        <section className="board-finished" aria-label="Finished work items">
          <h2>Finished · {shownFinished.length}</h2>
          {shownFinished.length === 0 ? (
            <p className="col-empty">No finished work items{q ? " match the filter" : ""}.</p>
          ) : (
            <WorkColumn title="Finished" tone="success" projectId={projectId} emptyLabel="No finished work items" showMoreLabel={(count) => `Show ${String(count)} more`}
              items={shownFinished.map((wi) => ({ id: wi.correlation_key, projectId: projectId ?? wi.pfactory.repo ?? wi.aifactory.repo ?? wi.tfactory.repo ?? "fleet", collapsedByDefault: true,
                render: (state) => <Card wi={wi} lp={progress[wi.correlation_key]} expanded={state.expanded} onToggle={() => { state.onToggle(); }} onOpen={() => setSelectedKey(wi.correlation_key)} /> }))} />
          )}
        </section>
      )}

      <AnimatePresence>
        {selected && (
          <TaskDetail
            wi={selected}
            lp={progress[selected.correlation_key]}
            onClose={() => setSelectedKey(null)}
            onActed={() => void apiRefresh()}
          />
        )}
      </AnimatePresence>
    </>
  );
}

function Card({ wi, lp, expanded, onToggle, onOpen }: { wi: WorkItem; lp?: LiveProgress | undefined; expanded: boolean; onToggle: () => void; onOpen: () => void }) {
  const state = itemState(wi);
  const title = displayTitle(wi.title, wi.correlation_key);
  const failed = hasFailedStage(wi);
  const phases: WorkPhase[] = STAGES.map(({ key, label }) => ({
    key, label,
    state: stageState(wi[key].status) === "failed" ? "failed" :
      stageState(wi[key].status) === "done" ? "done" :
      stageState(wi[key].status) === "running" ? "active" : "pending",
  }));
  const tone: WorkTone = failed ? "danger" : lp || state === "running" ? "running" :
    state === "review" || state === "queued" || state === "idle" ? "warning" : "success";
  return <WorkCard
    title={title}
    subtitle={`#${keySlug(wi.correlation_key)} · ${wi.timeline.length} events${lp?.percent == null && (lp?.subtask || lp?.phase || wi.tfactory.phase || wi.aifactory.phase || wi.pfactory.phase) ? ` · ${lp?.subtask || lp?.phase || wi.tfactory.phase || wi.aifactory.phase || wi.pfactory.phase}` : ""}`}
    status={STATE_LABEL[state]}
    tone={tone}
    age={relTime(wi.timeline.at(-1)?.updated_at ?? null)}
    progress={lp?.percent ?? undefined}
    phases={phases}
    expanded={expanded}
    onToggle={onToggle}
    onOpen={onOpen}
    actions={[{ id: "open", label: "Open", onClick: onOpen, variant: "primary" }]}
    collapsedByDefault={state === "done"}
  />;
}
