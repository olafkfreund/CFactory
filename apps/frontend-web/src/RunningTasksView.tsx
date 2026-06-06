import { useMemo, useState, type ComponentType, type SVGProps } from "react";
import { motion } from "framer-motion";
import type { LiveProgress, ServiceState, WorkItem } from "./api";
import { IconDocument, IconRobot, IconFlask } from "./icons";

type IconCmp = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;
type StageState = "done" | "running" | "failed" | "idle";
type Overall = "running" | "done" | "failed";
type Filter = "all" | Overall;

const STAGES: { key: keyof Pick<WorkItem, "pfactory" | "aifactory" | "tfactory">; label: string; cls: string; Icon: IconCmp }[] = [
  { key: "pfactory", label: "Plan", cls: "plan", Icon: IconDocument },
  { key: "aifactory", label: "Code", cls: "code", Icon: IconRobot },
  { key: "tfactory", label: "Test", cls: "test", Icon: IconFlask },
];

const DONE_RE = /(^|[_-])(merged|done|complete|completed|passed|approved|qa_approved|shipped|succeeded|success|ready)([_-]|$)/i;
const FAIL_RE = /(fail|reject|error|abort|cancel|blocked)/i;

function classify(status: string | null | undefined): StageState {
  if (!status) return "idle";
  if (FAIL_RE.test(status)) return "failed";
  if (DONE_RE.test(status)) return "done";
  return "running";
}

interface Row {
  wi: WorkItem;
  stageStates: StageState[];
  overall: Overall;
  activeIdx: number;
  percent: number | null; // null → indeterminate (running, % unknown)
  whatsLeft: string;
}

function buildRow(wi: WorkItem, lp: LiveProgress | undefined): Row | null {
  const slots: ServiceState[] = [wi.pfactory, wi.aifactory, wi.tfactory];
  const stageStates = slots.map((s) => classify(s.status));
  // Not started anywhere → not a "task" yet.
  if (stageStates.every((s) => s === "idle")) return null;

  const failedIdx = stageStates.findIndex((s) => s === "failed");
  // furthest engaged stage (last non-idle)
  let activeIdx = 0;
  stageStates.forEach((s, i) => { if (s !== "idle") activeIdx = i; });

  let overall: Overall;
  if (failedIdx >= 0) overall = "failed";
  else if (stageStates[2] === "done") overall = "done";
  else overall = "running";

  let percent: number | null;
  if (overall === "done") percent = 100;
  else if (lp && lp.percent != null) percent = Math.max(0, Math.min(100, lp.percent));
  else if (overall === "failed") percent = Math.round(((failedIdx + 0.6) / 3) * 100);
  else percent = null; // running, unknown → indeterminate

  const cur = slots[activeIdx];
  const whatsLeft =
    overall === "failed"
      ? `failed at ${STAGES[failedIdx >= 0 ? failedIdx : activeIdx].label.toLowerCase()} — ${slots[failedIdx >= 0 ? failedIdx : activeIdx].status}`
      : overall === "done"
        ? "all stages complete"
        : (lp?.subtask || lp?.phase || cur.phase || cur.status || "working…");

  return { wi, stageStates, overall, activeIdx, percent, whatsLeft };
}

export default function RunningTasksView({
  items,
  progress,
}: {
  items: WorkItem[];
  progress: Record<string, LiveProgress>;
}) {
  const [filter, setFilter] = useState<Filter>("all");

  const rows = useMemo(() => {
    const order: Record<Overall, number> = { failed: 0, running: 1, done: 2 };
    return items
      .map((wi) => buildRow(wi, progress[wi.correlation_key]))
      .filter((r): r is Row => r !== null)
      .sort((a, b) => order[a.overall] - order[b.overall]);
  }, [items, progress]);

  const counts = useMemo(() => {
    const c = { all: rows.length, running: 0, done: 0, failed: 0 };
    for (const r of rows) c[r.overall]++;
    return c;
  }, [rows]);

  const shown = filter === "all" ? rows : rows.filter((r) => r.overall === filter);

  const CHIPS: { id: Filter; label: string }[] = [
    { id: "all", label: "All" },
    { id: "running", label: "Running" },
    { id: "failed", label: "Failed" },
    { id: "done", label: "Done" },
  ];

  return (
    <>
      <div className="page-head">
        <h1>Running tasks</h1>
        <p>Live progress across every factory sibling — what's running, how far to done, what failed.</p>
      </div>

      <div className="rt-toolbar">
        {CHIPS.map((c) => (
          <button
            key={c.id}
            className={`rt-chip ${filter === c.id ? "active" : ""} rt-chip--${c.id}`}
            onClick={() => setFilter(c.id)}
          >
            {c.label} <span className="rt-chip-n">{counts[c.id]}</span>
          </button>
        ))}
      </div>

      {shown.length === 0 ? (
        <div className="rt-empty">Nothing {filter === "all" ? "in flight" : filter} right now.</div>
      ) : (
        <div className="rt-grid">
          {shown.map((r, i) => (
            <motion.div
              key={r.wi.correlation_key}
              className={`rt-card rt-card--${r.overall}`}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.25, delay: Math.min(i * 0.025, 0.3) }}
            >
              <div className="rt-head">
                <span className="rt-ident">
                  <span className="rt-key">#{r.wi.correlation_key}</span>
                  {r.wi.title && <span className="rt-title">{r.wi.title}</span>}
                </span>
                <span className={`status-pill ${r.overall === "done" ? "ok" : r.overall === "failed" ? "fail" : "run"}`}>
                  <span className="dot" /> {r.overall}
                </span>
              </div>

              <div className="rt-stages">
                {STAGES.map((s, idx) => {
                  const Icon = s.Icon;
                  const st = r.stageStates[idx];
                  return (
                    <div className="rt-stage-wrap" key={s.key}>
                      {idx > 0 && <span className={`rt-link ${r.stageStates[idx - 1] === "done" ? "filled" : ""}`} />}
                      <div
                        className={`rt-stage rt-stage--${s.cls} is-${st} ${idx === r.activeIdx && r.overall === "running" ? "is-active" : ""}`}
                        title={`${s.label}: ${r.wi[s.key].status ?? "—"}`}
                      >
                        <Icon size={16} />
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className={`rt-bar rt-bar--${r.overall} ${r.percent === null ? "rt-bar--indet" : ""}`}>
                {r.percent === null ? (
                  <span className="rt-bar-indet" />
                ) : (
                  <motion.span
                    className="rt-bar-fill"
                    initial={{ width: 0 }}
                    animate={{ width: `${r.percent}%` }}
                    transition={{ duration: 0.6, ease: "easeOut" }}
                  />
                )}
              </div>

              <div className="rt-foot">
                <span className="rt-pct">{r.percent === null ? "in progress" : `${Math.round(r.percent)}%`}</span>
                <span className="rt-left" title={r.whatsLeft}>{r.whatsLeft}</span>
              </div>
            </motion.div>
          ))}
        </div>
      )}
    </>
  );
}
