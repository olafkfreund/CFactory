import { useEffect, useMemo, useState, type ComponentType, type SVGProps } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { fetchLiveAgents, fetchTokens, fetchTokensByWorker, fetchWorkerProgress, refresh as apiRefresh, type LiveAgent, type LiveProgress, type ProgressSample, type ServiceState, type WorkerRow, type WorkItem } from "./api";
import { IconDocument, IconRobot, IconFlask } from "./icons";
import { stageState, overallState, STATE_LABEL, STATE_PILL, type TaskState } from "./taskState";
import { displayTitle, keySlug } from "./correlationKey";
import TaskDetail from "./TaskDetail";
import AgentConsoleModal from "./AgentConsoleModal";
import LiveTaskStamp from "./LiveTaskStamp";
import { fmtAge } from "./format";

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
  stalled: boolean; // #105: hung in a non-terminal stage past the idle deadline
}

function buildRow(wi: WorkItem, lp: LiveProgress | undefined): Row | null {
  const slots: ServiceState[] = [wi.pfactory, wi.aifactory, wi.tfactory];
  const stageStates = slots.map((s) => stageState(s.status));
  if (stageStates.every((s) => s === "idle")) return null; // never started → not a task

  const overall = overallState(stageStates);

  let activeIdx = 0;
  stageStates.forEach((s, i) => { if (s !== "idle") activeIdx = i; });
  const failedIdx = stageStates.findIndex((s) => s === "failed");

  // Honest numbers only: real live percent while running, 100 when done.
  // A failed task gets a broken-bar treatment, never a synthetic "53%".
  let percent: number | null;
  if (overall === "done") percent = 100;
  else if (overall === "running" && lp && lp.percent != null) percent = Math.max(0, Math.min(100, lp.percent));
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

  // A stalled task (#105) is still "running" to the stage machine but the
  // watchdog/last-activity says no movement past the deadline — surface it.
  const stalled = (wi.liveness?.stalled ?? false) && overall === "running";

  return { wi, stageStates, overall, activeIdx, percent, whatsLeft, stalled };
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
  // Per-worker usage rows keyed by correlation_key (Tier 1 live cost stamp).
  // Polled on the same 5s cadence as live agents; the WS WorkItem broadcast
  // carries no per-worker breakdown, so we fetch /api/tokens/by_worker here.
  // Best-effort: a failed fetch leaves the previous map in place and never
  // blanks a card — a task with no rows simply shows no stamp (as before).
  const [workersByKey, setWorkersByKey] = useState<Map<string, WorkerRow[]>>(new Map());
  // Tier 1.5: dense per-~10s heartbeat series keyed by correlation_key, for the
  // smooth ticking sparkline. Fetched per running task on the same cadence,
  // best-effort: a failed fetch keeps the last-good series (never blanks a card),
  // and a task with no series simply falls back to the stepwise stamp.
  const [progressByKey, setProgressByKey] = useState<Map<string, ProgressSample[]>>(new Map());
  // Scalar-aggregate cost/tokens per task (from /api/tokens). The per-worker
  // split can be null (attribution gap), so this is the authoritative total the
  // stamp shows as a fallback. Best-effort, same cadence.
  const [costByKey, setCostByKey] = useState<Map<string, { costUsd: number; totalTokens: number; metered: boolean }>>(new Map());

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

  // Per-worker usage poll (Tier 1). Same cadence; additive + best-effort.
  useEffect(() => {
    let alive = true;
    const poll = () =>
      fetchTokensByWorker()
        .then((r) => {
          if (!alive) return;
          const m = new Map<string, WorkerRow[]>();
          for (const row of r.by_work_item) m.set(row.correlation_key, row.workers);
          setWorkersByKey(m);
        })
        .catch(() => undefined); // keep last good map; never blank a card
    poll();
    const id = window.setInterval(poll, 5000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  // Scalar-aggregate cost/tokens poll. Same cadence; additive + best-effort.
  useEffect(() => {
    let alive = true;
    const poll = () =>
      fetchTokens()
        .then((r) => {
          if (!alive) return;
          const m = new Map<string, { costUsd: number; totalTokens: number; metered: boolean }>();
          for (const row of r.by_work_item) {
            // Conservative (#96): only treat as metered when the billing summary
            // positively says so — otherwise the stamp shows tokens + time, no $.
            m.set(row.correlation_key, {
              costUsd: row.cost_usd,
              totalTokens: row.total_tokens,
              metered: row.billing?.has_metered ?? false,
            });
          }
          setCostByKey(m);
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

  // Keys of currently-running tasks — the only ones with a live heartbeat
  // series worth polling. Joined into a stable string so the poll effect
  // re-subscribes only when the running set actually changes.
  const runningKeys = useMemo(
    () => rows.filter((r) => r.overall === "running").map((r) => r.wi.correlation_key),
    [rows],
  );
  const runningKeysSig = runningKeys.join(",");

  // Per-running-task heartbeat poll (Tier 1.5). Same 5s cadence; additive +
  // best-effort. Each task is fetched independently; a single failure leaves
  // that task's last-good series untouched and never blanks others.
  useEffect(() => {
    if (runningKeys.length === 0) return;
    let alive = true;
    const poll = () => {
      for (const key of runningKeys) {
        fetchWorkerProgress(key)
          .then((wp) => {
            if (!alive || wp.series.length === 0) return; // keep last-good on empty
            setProgressByKey((prev) => {
              const next = new Map(prev);
              next.set(key, wp.series);
              return next;
            });
          })
          .catch(() => undefined); // keep last good series; never blank a card
      }
    };
    poll();
    const id = window.setInterval(poll, 5000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runningKeysSig]);

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
        <h1>Active tasks</h1>
        <p>
          In-flight work across every factory sibling — {counts.running} running,{" "}
          {counts.review} in review{counts.active - counts.running - counts.review - counts.failed > 0
            ? `, ${counts.active - counts.running - counts.review - counts.failed} queued`
            : ""}
          . Most are awaiting review, not actively running. Finished tasks drop out — click a card for
          detail, open the console while an agent is live.
        </p>
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
            const workers = workersByKey.get(r.wi.correlation_key);
            const wprogress = progressByKey.get(r.wi.correlation_key);
            return (
              <motion.div
                key={r.wi.correlation_key}
                className={`rt-card rt-card--${r.overall} rt-card--clickable ${r.stalled ? "rt-card--stalled" : ""}`}
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
                    <span className="rt-key" title={r.wi.correlation_key}>#{keySlug(r.wi.correlation_key)}</span>
                    <span className="rt-title">{displayTitle(r.wi.title, r.wi.correlation_key)}</span>
                  </span>
                  {r.stalled && (
                    <span
                      className="rt-stalled-badge"
                      title={`No activity for ${fmtAge(r.wi.liveness?.last_activity_age_seconds ?? 0)} while ${r.wi.liveness?.active_status ?? "active"} — may be stuck`}
                    >
                      STALLED · {fmtAge(r.wi.liveness?.last_activity_age_seconds ?? 0)}
                    </span>
                  )}
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

                <div
                  className={`rt-bar rt-bar--${r.overall} ${r.percent === null && r.overall !== "failed" ? "rt-bar--indet" : ""}`}
                >
                  {r.overall === "failed" ? (
                    <span className="rt-bar-broken" title={r.whatsLeft} />
                  ) : r.percent === null ? (
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

                {/* Tier 1 live cost/usage stamp + sparkline. Renders nothing
                    until this task has per-worker data, so cards without it are
                    unchanged. */}
                <LiveTaskStamp
                  wi={r.wi}
                  workers={workers}
                  progress={wprogress}
                  fallback={costByKey.get(r.wi.correlation_key)}
                  metered={costByKey.get(r.wi.correlation_key)?.metered}
                />
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
            onActed={() => void apiRefresh()}
          />
        )}
      </AnimatePresence>

      {consoleAgent && (
        <AgentConsoleModal agent={consoleAgent} onClose={() => setConsoleAgent(null)} />
      )}
    </>
  );
}
