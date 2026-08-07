// The Pipeline board view (plan → code → test columns), its work-item cards and
// the live/stage chips — extracted from App.tsx (#115). Pure extraction —
// behaviour-preserving, no logic or UI change.
import { useCallback, useMemo, useState, type CSSProperties } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { refresh as apiRefresh, type LiveProgress, type ServiceState, type WorkItem } from "./api";
import TaskDetail from "./TaskDetail";
import type { StripStage } from "./PipelineStrip";
import { activeStage, STATE_LABEL } from "./taskState";
import { displayTitle, keySlug } from "./correlationKey";
import { IconClock } from "./icons";
import { itemState, relTime, STAGES, type StageKey } from "./dashboard";

type BoardChip = "all" | "running" | "review" | "queued" | "failed";

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
}: {
  items: WorkItem[];
  progress: Record<string, LiveProgress>;
  stageFilter: StripStage | null;
  onClearStage: () => void;
}) {
  const [query, setQuery] = useState("");
  const [chip, setChip] = useState<BoardChip>("all");
  const [showFinished, setShowFinished] = useState(false);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  // Same lifecycle semantics as the Mission Control ring and the pipeline
  // strip: an item lives at its furthest ACTIVE stage; finished/idle items
  // drop into the (hidden by default) finished section instead of inflating
  // the columns.
  const { byStage, finished } = useMemo(() => {
    const m: Record<StageKey, WorkItem[]> = { pfactory: [], aifactory: [], tfactory: [] };
    const finished: WorkItem[] = [];
    for (const wi of items) {
      const stage = activeStage({
        pfactory: wi.pfactory.status,
        aifactory: wi.aifactory.status,
        tfactory: wi.tfactory.status,
      });
      if (stage) m[stage].push(wi);
      else finished.push(wi);
    }
    return { byStage: m, finished };
  }, [items]);

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
    (wi: WorkItem) => matchesQuery(wi) && (chip === "all" || itemState(wi) === chip),
    [matchesQuery, chip],
  );

  const chipCounts = useMemo(() => {
    const active = [...byStage.pfactory, ...byStage.aifactory, ...byStage.tfactory].filter(
      matchesQuery,
    );
    const c: Record<BoardChip, number> = {
      all: active.length,
      running: 0,
      review: 0,
      queued: 0,
      failed: 0,
    };
    for (const wi of active) {
      const st = itemState(wi);
      if (st === "running" || st === "review" || st === "queued" || st === "failed") c[st]++;
    }
    return c;
  }, [byStage, matchesQuery]);

  const stages = STAGES.filter((s) => !stageFilter || s.key === stageFilter);
  const shownFinished = finished.filter(matchesQuery);

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
            onClick={() => setChip(c.id)}
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
            <div className={`column column--${stage.cls}`} key={stage.key}>
              <div className="column-head">
                <span className="column-dot" />
                <span className="column-title">{stage.label}</span>
                <span className="column-svc">{stage.svc}</span>
                <span className="column-count">{cards.length}</span>
              </div>
              <div className="column-body">
                {cards.length === 0 ? (
                  <p className="col-empty">
                    {byStage[stage.key].length === 0
                      ? "No active work items"
                      : "Nothing matches the filter"}
                  </p>
                ) : (
                  cards.map((wi) => (
                    <Card
                      key={wi.correlation_key}
                      wi={wi}
                      lp={progress[wi.correlation_key]}
                      onOpen={() => setSelectedKey(wi.correlation_key)}
                    />
                  ))
                )}
              </div>
            </div>
          );
        })}
      </section>

      {showFinished && (
        <section className="board-finished" aria-label="Finished work items">
          <h2>Finished · {shownFinished.length}</h2>
          {shownFinished.length === 0 ? (
            <p className="col-empty">No finished work items{q ? " match the filter" : ""}.</p>
          ) : (
            <div className="board-finished-grid">
              {shownFinished.map((wi) => (
                <Card
                  key={wi.correlation_key}
                  wi={wi}
                  lp={progress[wi.correlation_key]}
                  onOpen={() => setSelectedKey(wi.correlation_key)}
                />
              ))}
            </div>
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

function Card({
  wi,
  lp,
  onOpen,
}: {
  wi: WorkItem;
  lp?: LiveProgress | undefined;
  onOpen: () => void;
}) {
  const last = wi.timeline.at(-1)?.updated_at ?? null;
  const state = itemState(wi);
  return (
    <article
      className={`card-wi card-wi--clickable ${lp ? "card-wi--live" : ""}`}
      onClick={onOpen}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onOpen()}
    >
      <div className="card-wi__head">
        <span className="wi-key" title={wi.correlation_key}>
          #{keySlug(wi.correlation_key)}
        </span>
        {lp ? (
          <LiveBadge lp={lp} />
        ) : (
          last && (
            <span className="wi-time">
              <IconClock /> {relTime(last)}
            </span>
          )
        )}
      </div>
      <div className="card-wi__title">{displayTitle(wi.title, wi.correlation_key)}</div>
      <div className="stage-chips">
        {STAGES.map((s) => (
          <StageChip key={s.key} label={s.label} cls={s.cls} state={wi[s.key]} />
        ))}
      </div>
      <div className="card-wi__foot">
        {wi.timeline.length} events · {STATE_LABEL[state]}
      </div>
    </article>
  );
}

function LiveBadge({ lp }: { lp: LiveProgress }) {
  return (
    <motion.span
      className="live-badge"
      title={lp.subtask || lp.phase || "active"}
      animate={{ opacity: [1, 0.55, 1] }}
      transition={{ duration: 1.6, repeat: Infinity, ease: "easeInOut" }}
    >
      <span className="live-badge-dot" />
      {lp.percent != null ? `${Math.round(lp.percent)}%` : lp.phase || "live"}
    </motion.span>
  );
}

function StageChip({ label, cls, state }: { label: string; cls: string; state: ServiceState }) {
  const active = Boolean(state && state.status);
  return (
    <span
      className={`schip ${active ? `schip--${cls}` : "schip--idle"}`}
      title={state?.task_id ?? ""}
    >
      <span className="schip-k">{label}</span>
      <span className="schip-v">{active ? state.status : "—"}</span>
    </span>
  );
}
