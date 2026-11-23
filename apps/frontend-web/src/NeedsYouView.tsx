// Needs-you inbox (#148) — one list of everything across the fleet that is
// blocked on a human: plan approvals, verify/review gates, and stalls. Derived
// from the live work-item list (needsYou.ts does the classification), so it needs
// no new backend. Each row opens the task in TaskDetail, where the real Approve /
// Reject / Unstick actions already live — the inbox routes attention, it doesn't
// duplicate those controls.
import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { refresh as apiRefresh, type LiveProgress, type WorkItem } from "./api";
import { displayTitle } from "./correlationKey";
import {
  attentionList,
  loadDismissedNeedsYou,
  saveDismissedNeedsYou,
  type Attention,
  type AttentionKind,
} from "./needsYou";
import TaskDetail from "./TaskDetail";

const STAGE_VAR: Record<string, string> = {
  plan: "var(--plan)",
  code: "var(--code)",
  test: "var(--test)",
};

const KIND_LABEL: Record<AttentionKind, string> = {
  approval: "Approval",
  review: "Review gate",
  stalled: "Stalled",
};

type FilterKey = "all" | AttentionKind;

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: "all", label: "All" },
  { key: "approval", label: "Approvals" },
  { key: "review", label: "Review gates" },
  { key: "stalled", label: "Stalled" },
];

export default function NeedsYouView({
  items,
  progress,
  projectScoped = false,
  now,
}: {
  items: WorkItem[];
  progress: Record<string, LiveProgress>;
  projectScoped?: boolean | undefined;
  now?: number | undefined;
}) {
  const [filter, setFilter] = useState<FilterKey>("all");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState<Set<string>>(() => loadDismissedNeedsYou());
  const [showHidden, setShowHidden] = useState(false);
  const [clock, setClock] = useState(() => now ?? Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setClock(now ?? Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, [now]);

  const all = useMemo(
    () => attentionList(items, { now: now ?? clock }),
    [items, now, clock],
  );
  const hideKey = (a: Attention) => `${a.wi.correlation_key}:${a.kind}:${a.stage}`;
  const hiddenCount = useMemo(
    () => all.filter((a) => dismissed.has(`${a.wi.correlation_key}:${a.kind}:${a.stage}`)).length,
    [all, dismissed],
  );
  const visibleAll = useMemo(
    () => (showHidden ? all : all.filter((a) => !dismissed.has(`${a.wi.correlation_key}:${a.kind}:${a.stage}`))),
    [all, dismissed, showHidden],
  );
  const counts = useMemo(() => {
    const c: Record<AttentionKind, number> = { approval: 0, review: 0, stalled: 0 };
    for (const a of visibleAll) c[a.kind] += 1;
    return c;
  }, [visibleAll]);
  const shown = filter === "all" ? visibleAll : visibleAll.filter((a) => a.kind === filter);
  const recent = shown.filter((a) => !a.older);
  const older = shown.filter((a) => a.older);

  const toggleDismiss = (key: string) => {
    setDismissed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      saveDismissedNeedsYou(next);
      return next;
    });
  };

  const selected = selectedKey
    ? (items.find((w) => w.correlation_key === selectedKey) ?? null)
    : null;

  const renderRow = (a: Attention) => {
    const isHidden = dismissed.has(hideKey(a));
    return (
      <motion.div
        key={a.wi.correlation_key}
        className={`ny-item ny-item--${a.kind}${isHidden ? " ny-item--hidden" : ""}`}
        initial={{ opacity: 0, y: 4 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2 }}
      >
        <span
          className="ny-src"
          style={{ background: STAGE_VAR[a.stage], color: STAGE_VAR[a.stage] }}
        />
        <div className="ny-body">
          <h3 className="ny-item-title">{displayTitle(a.wi.title, a.wi.correlation_key)}</h3>
          <p className="ny-why">{a.reason}</p>
          <div className="ny-meta">
            <span>{a.service}</span>
            <span className="ny-tag">{KIND_LABEL[a.kind]}</span>
            <span className="ny-key">#{a.wi.correlation_key}</span>
            <span className="ny-age" title={a.lastActivityAt ?? "No activity timestamp"}>
              {a.age}
            </span>
          </div>
        </div>
        <div className="ny-actions">
          <button
            type="button"
            className="ny-open f-btn f-btn--sm f-btn--primary"
            onClick={() => {
              setSelectedKey(a.wi.correlation_key);
            }}
          >
            Open
          </button>
          <button
            type="button"
            className="ny-dismiss f-btn f-btn--sm f-btn--outline"
            onClick={() => {
              toggleDismiss(hideKey(a));
            }}
          >
            {isHidden ? "Restore" : "Dismiss"}
          </button>
        </div>
      </motion.div>
    );
  };

  return (
    <section className="ny">
      <div className="ny-head">
        <h1 className="ny-title">Needs you</h1>
        <p className="ny-sub">
          {projectScoped ? "Project tasks" : "Everything across the fleet"} blocked on a human — plan approvals, review gates and
          stalls, most-actionable first.
        </p>
      </div>

      <div className="ny-filters" role="tablist" aria-label="Attention filter">
        {FILTERS.map((f) => {
          const n = f.key === "all" ? visibleAll.length : counts[f.key];
          return (
            <button
              key={f.key}
              type="button"
              role="tab"
              aria-selected={filter === f.key}
              className={`ny-chip ${filter === f.key ? "on" : ""}`}
              onClick={() => {
                setFilter(f.key);
              }}
            >
              {f.label} · {n}
            </button>
          );
        })}
        {hiddenCount > 0 && (
          <button
            type="button"
            className={`ny-chip ${showHidden ? "on" : ""}`}
            aria-pressed={showHidden}
            onClick={() => {
              setShowHidden((v) => !v);
            }}
          >
            {showHidden ? `Hide hidden (${String(hiddenCount)})` : `Show hidden (${String(hiddenCount)})`}
          </button>
        )}
      </div>

      {shown.length === 0 ? (
        <div className="ny-empty">
          {all.length === 0
            ? "Nothing needs you right now — every task is either flowing or finished."
            : "Nothing matches this filter."}
        </div>
      ) : (
        <div className="ny-list">
          {recent.map(renderRow)}
          {older.length > 0 && (
            <details className="ny-older">
              <summary className="ny-older-sum">Older ({older.length})</summary>
              <div className="ny-list ny-older-list">{older.map(renderRow)}</div>
            </details>
          )}
        </div>
      )}

      <AnimatePresence>
        {selected && (
          <TaskDetail
            wi={selected}
            lp={progress[selected.correlation_key]}
            onClose={() => {
              setSelectedKey(null);
            }}
            onActed={() => {
              void apiRefresh();
            }}
          />
        )}
      </AnimatePresence>
    </section>
  );
}
