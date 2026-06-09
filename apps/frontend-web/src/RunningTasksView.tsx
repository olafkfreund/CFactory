import { useEffect, useMemo, useState, type ComponentType, type SVGProps } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { fetchLiveAgents, type LiveAgent, type LiveProgress, type ServiceState, type WorkItem } from "./api";
import { IconDocument, IconRobot, IconFlask } from "./icons";
import { stageState, overallState, STATE_LABEL, STATE_PILL, type TaskState } from "./taskState";
import TaskDetail from "./TaskDetail";
import AgentConsoleModal from "./AgentConsoleModal";

type IconCmp = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;
type Filter = "active" | "running" | "review" | "failed" | "done";

const STAGES: { key: keyof Pick<WorkItem, "pfactory" | "aifactory" | "tfactory">; label: string; cls: string; Icon: IconCmp }[] = [
  { key: "pfactory", label: "Plan", cls: "plan", Icon: IconDocument },
  { key: "aifactory", label: "Code", cls: "code", Icon: IconRobot },
  { key: "tfactory", label: "Test", cls: "test", Icon: IconFlask },
];

interface Row {
  wi: WorkItem;
  stageStates: TaskState[];
  overall: TaskState;
  activeIdx: number;
  percent: number | null; // null → indeterminate
  whatsLeft: string;
}

function buildRow(wi: WorkItem, lp: LiveProgress | undefined): Row | null {
  const slots: ServiceState[] = [wi.pfactory, wi.aifactory, wi.tfactory];
  const stageStates = slots.map((s) => stageState(s.status));
  if (stageStates.every((s) => s === "idle")) return null; // never started → not a task

  const overall = overallState(stageStates);

  let activeIdx = 0;
  stageStates.forEach((s, i) => { if (s !== "idle") activeIdx = i; });
  const failedIdx = stageStates.findIndex((s) => s === "failed");

  let percent: number | null;
  if (overall === "done") percent = 100;
  else if (overall === "running" && lp && lp.percent != null) percent = Math.max(0, Math.min(100, lp.percent));
  else if (overall === "failed") percent = Math.round(((failedIdx + 0.6) / 3) * 100);
  else percent = null;

  const cur = slots[activeIdx];
  const whatsLeft =
    overall === "failed"
      ? `failed at ${STAGES[failedIdx >= 0 ? failedIdx : activeIdx].label.toLowerCase()} — ${slots[failedIdx >= 0 ? failedIdx : activeIdx].status}`
      : overall === "done"
        ? "all stages complete"
        : overall === "review"
          ? "awaiting review"
          : overall === "queued"
            ? "queued"
            : (lp?.subtask || lp?.phase || cur.phase || cur.status || "working…");

  return { wi, stageStates, overall, activeIdx, percent, whatsLeft };
}

const ORDER: Record<TaskState, number> = { failed: 0, running: 1, review: 2, queued: 3, done: 4, idle: 5 };

export default function RunningTasksView({
  items,
  progress,
}: {
  items: WorkItem[];
  progress: Record<string, LiveProgress>;
}) {
  const [filter, setFilter] = useState<Filter>("active");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [consoleAgent, setConsoleAgent] = useState<LiveAgent | null>(null);
  const [liveAgents, setLiveAgents] = useState<Map<string, LiveAgent>>(new Map());

  // Which work items currently have a live, streamable rmux agent — drives the
  // "open console" affordance. Polled so it tracks tasks starting/finishing.
  useEffect(() => {
    let alive = true;
    const poll = () =>
      fetchLiveAgents()
        .then((r) => {
          if (alive) setLiveAgents(new Map(r.agents.map((a) => [a.correlation_key, a])));
        })
        .catch(() => undefined);
    poll();
    const id = window.setInterval(poll, 5000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  const rows = useMemo(
    () =>
      items
        .map((wi) => buildRow(wi, progress[wi.correlation_key]))
        .filter((r): r is Row => r !== null)
        .sort((a, b) => ORDER[a.overall] - ORDER[b.overall]),
    [items, progress],
  );

  const counts = useMemo(() => {
    const c = { active: 0, running: 0, review: 0, failed: 0, done: 0 };
    for (const r of rows) {
      if (r.overall !== "done") c.active++;
      if (r.overall === "running") c.running++;
      else if (r.overall === "review") c.review++;
      else if (r.overall === "failed") c.failed++;
      else if (r.overall === "done") c.done++;
    }
    return c;
  }, [rows]);

  const shown = rows.filter((r) =>
    filter === "active" ? r.overall !== "done" : r.overall === filter,
  );

  const CHIPS: { id: Filter; label: string }[] = [
    { id: "active", label: "Active" },
    { id: "running", label: "Running" },
    { id: "review", label: "In review" },
    { id: "failed", label: "Failed" },
    { id: "done", label: "Done" },
  ];

  const selected = selectedKey ? items.find((w) => w.correlation_key === selectedKey) ?? null : null;

  return (
    <>
      <div className="page-head">
        <h1>Running tasks</h1>
        <p>Live work across every factory sibling. Finished tasks drop out of Active — click a card for detail, open the console while an agent is live.</p>
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
        <div className="rt-empty">Nothing {filter === "active" ? "in flight" : filter} right now.</div>
      ) : (
        <div className="rt-grid">
          {shown.map((r, i) => {
            const liveAgent = liveAgents.get(r.wi.correlation_key);
            const canConsole = r.overall === "running" && liveAgent != null;
            return (
              <motion.div
                key={r.wi.correlation_key}
                className={`rt-card rt-card--${r.overall} rt-card--clickable`}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.25, delay: Math.min(i * 0.025, 0.3) }}
                onClick={() => setSelectedKey(r.wi.correlation_key)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && setSelectedKey(r.wi.correlation_key)}
              >
                <div className="rt-head">
                  <span className="rt-ident">
                    <span className="rt-key">#{r.wi.correlation_key}</span>
                    {r.wi.title && <span className="rt-title">{r.wi.title}</span>}
                  </span>
                  <span className={`status-pill ${STATE_PILL[r.overall]}`}>
                    <span className="dot" /> {STATE_LABEL[r.overall]}
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
                  <span className="rt-pct">{r.percent === null ? STATE_LABEL[r.overall] : `${Math.round(r.percent)}%`}</span>
                  <span className="rt-left" title={r.whatsLeft}>{r.whatsLeft}</span>
                  {canConsole && (
                    <button
                      className="rt-console-btn"
                      title="Open live agent console"
                      onClick={(e) => {
                        e.stopPropagation();
                        setConsoleAgent(liveAgent);
                      }}
                    >
                      ▸ console
                    </button>
                  )}
                </div>
              </motion.div>
            );
          })}
        </div>
      )}

      <AnimatePresence>
        {selected && (
          <TaskDetail
            wi={selected}
            lp={progress[selected.correlation_key]}
            onClose={() => setSelectedKey(null)}
          />
        )}
      </AnimatePresence>

      {consoleAgent && (
        <AgentConsoleModal agent={consoleAgent} onClose={() => setConsoleAgent(null)} />
      )}
    </>
  );
}
