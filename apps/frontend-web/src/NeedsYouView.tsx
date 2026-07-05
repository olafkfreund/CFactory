// Needs-you inbox (#148) — one list of everything across the fleet that is
// blocked on a human: plan approvals, verify/review gates, and stalls. Derived
// from the live work-item list (needsYou.ts does the classification), so it needs
// no new backend. Each row opens the task in TaskDetail, where the real Approve /
// Reject / Unstick actions already live — the inbox routes attention, it doesn't
// duplicate those controls.
import { useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { refresh as apiRefresh, type LiveProgress, type WorkItem } from "./api";
import { displayTitle } from "./correlationKey";
import { attentionList, type AttentionKind } from "./needsYou";
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
}: {
  items: WorkItem[];
  progress: Record<string, LiveProgress>;
}) {
  const [filter, setFilter] = useState<FilterKey>("all");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  const all = useMemo(() => attentionList(items), [items]);
  const counts = useMemo(() => {
    const c: Record<AttentionKind, number> = { approval: 0, review: 0, stalled: 0 };
    for (const a of all) c[a.kind] += 1;
    return c;
  }, [all]);
  const shown = filter === "all" ? all : all.filter((a) => a.kind === filter);

  const selected = selectedKey
    ? (items.find((w) => w.correlation_key === selectedKey) ?? null)
    : null;

  return (
    <section className="ny">
      <div className="ny-head">
        <h1 className="ny-title">Needs you</h1>
        <p className="ny-sub">
          Everything across the fleet that's blocked on a human — plan approvals, review gates and
          stalls, most-actionable first.
        </p>
      </div>

      <div className="ny-filters" role="tablist" aria-label="Attention filter">
        {FILTERS.map((f) => {
          const n = f.key === "all" ? all.length : counts[f.key];
          return (
            <button
              key={f.key}
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
      </div>

      {shown.length === 0 ? (
        <div className="ny-empty">
          {all.length === 0
            ? "Nothing needs you right now — every task is either flowing or finished."
            : "Nothing matches this filter."}
        </div>
      ) : (
        <div className="ny-list">
          {shown.map((a) => (
            <motion.div
              key={a.wi.correlation_key}
              className={`ny-item ny-item--${a.kind}`}
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
                </div>
              </div>
              <button
                className="ny-open"
                onClick={() => {
                  setSelectedKey(a.wi.correlation_key);
                }}
              >
                Open
              </button>
            </motion.div>
          ))}
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
