import { useCallback, useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  type CostRouting,
  evidenceMediaUrl,
  fetchCostRouting,
  fetchLiveAgents,
  fetchProcess,
  fetchTokens,
  fetchTokensByWorker,
  fetchWorkerProgress,
  type LiveAgent,
  type LiveProgress,
  type ProcessDetail,
  type ProgressSample,
  type WorkerRow,
  type WorkItem,
} from "./api";
import { AgentTerminal } from "./LiveAgents";
import TaskActions from "./TaskActions";
import CostRoutingPanel from "./CostRoutingPanel";
import StageGates from "./StageGates";
import TraceabilityPanel from "./TraceabilityPanel";
import ArtifactsPanel from "./ArtifactsPanel";
import LiveTaskStamp from "./LiveTaskStamp";
import { StageFlow } from "./TaskFlow";
import { displayTitle, keySlug } from "./correlationKey";
import { stageState } from "./taskState";

const STAGES = [
  { key: "pfactory", label: "Plan", svc: "PFactory" },
  { key: "aifactory", label: "Code", svc: "AIFactory" },
  { key: "tfactory", label: "Test", svc: "TFactory" },
] as const;

// Keep the modal honest *and* fresh: re-fetch process/live-agent state on this
// cadence while it's open, so a modal left up on the always-on monitor never
// shows stale truth.
const POLL_MS = 4000;

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function Bar({ pct, label, failed }: { pct: number | null | undefined; label: string; failed?: boolean }) {
  const v = typeof pct === "number" ? Math.max(0, Math.min(100, pct)) : null;
  return (
    <div className="td-bar">
      <div className="td-bar-label">
        <span>{label}</span>
        <span>{v == null ? "—" : `${Math.round(v)}%${failed ? " · failed" : ""}`}</span>
      </div>
      <div className="td-bar-track">
        <div
          className={`td-bar-fill ${failed ? "td-bar-fill--failed" : ""}`}
          style={{ width: `${v ?? 0}%` }}
        />
      </div>
    </div>
  );
}

/** Segmented progress when subtasks failed: green = completed, red = failed,
 *  track = remaining. Never a clean 100% when something broke. */
function SegmentedBar({ done, failed, total }: { done: number; failed: number; total: number }) {
  const dp = total > 0 ? (done / total) * 100 : 0;
  const fp = total > 0 ? (failed / total) * 100 : 0;
  return (
    <div className="td-bar">
      <div className="td-bar-label">
        <span>Overall</span>
        <span>
          {done}/{total} done · {failed} failed
        </span>
      </div>
      <div className="td-bar-track td-bar-track--seg">
        <div className="td-seg td-seg--done" style={{ width: `${dp}%` }} />
        <div className="td-seg td-seg--failed" style={{ width: `${fp}%` }} />
      </div>
    </div>
  );
}

export default function TaskDetail({
  wi,
  lp,
  onClose,
  onActed,
}: {
  wi: WorkItem;
  // `| undefined` is deliberate under exactOptionalPropertyTypes: callers read
  // these out of a lookup (`progress[key]`) and genuinely hand over an explicit
  // undefined, which is a different thing from omitting the prop. Same for every
  // other optional prop in this tree that a caller forwards rather than omits.
  lp?: LiveProgress | undefined;
  onClose: () => void;
  onActed?: (() => void) | undefined;
}) {
  const [proc, setProc] = useState<ProcessDetail | null>(null);
  const [agent, setAgent] = useState<LiveAgent | null>(null);
  // Per-worker usage rows for this task (Tier 1 live cost stamp). Best-effort,
  // additive: a failed fetch leaves it null and the stamp simply doesn't show.
  const [workers, setWorkers] = useState<WorkerRow[] | null>(null);
  // Tier 1.5: dense per-~10s heartbeat series for the smooth ticking sparkline.
  // Best-effort + additive: kept null until a non-empty series lands; on empty
  // or error the stamp falls back to the stepwise worker-completion series.
  const [progress, setProgress] = useState<ProgressSample[] | null>(null);
  // Whether this task has real metered (api/cloud) spend — drives the stamp's
  // cost vs tokens+time display (#96). undefined until the first tokens fetch.
  const [metered, setMetered] = useState<boolean | undefined>(undefined);
  // RFC-0014 (#124): pre-execution routing estimate vs actual rolled-up spend.
  // Best-effort + additive: a 404 (unknown task) or error leaves it null and the
  // Cost & routing panel simply doesn't render.
  const [costRouting, setCostRouting] = useState<CostRouting | null>(null);
  const [copied, setCopied] = useState(false);
  const key = wi.correlation_key;
  const dialogRef = useRef<HTMLDivElement>(null);
  // Set by TaskActions while the reject-reason textarea has text: an overlay
  // click must not throw that away. Escape still closes.
  const reasonDirtyRef = useRef(false);

  useEffect(() => {
    let alive = true;
    const load = () => {
      fetchProcess(key)
        .then((p) => alive && setProc(p))
        .catch(() => alive && setProc({ available: false, correlation_key: key }));
      fetchLiveAgents()
        .then((r) => alive && setAgent(r.agents.find((a) => a.correlation_key === key) ?? null))
        .catch(() => alive && setAgent(null));
      fetchTokensByWorker()
        .then((r) => {
          if (!alive) return;
          const row = r.by_work_item.find((w) => w.correlation_key === key);
          setWorkers(row ? row.workers : null);
        })
        .catch(() => undefined); // additive; keep prior value, never blank
      fetchWorkerProgress(key)
        .then((wp) => {
          if (!alive || wp.series.length === 0) return; // keep last-good on empty
          setProgress(wp.series);
        })
        .catch(() => undefined); // additive; keep prior series, never blank
      // Billing mode (#96): the stamp shows cost only when this task has real
      // metered spend; otherwise tokens + time. Best-effort, same source as the
      // Usage-by-task panel.
      fetchTokens()
        .then((r) => {
          if (!alive) return;
          const row = r.by_work_item.find((w) => w.correlation_key === key);
          setMetered(row?.billing?.has_metered);
        })
        .catch(() => undefined);
      // RFC-0014 (#124): estimate-vs-actual + routing rationale for this task.
      // Best-effort; a 404/error keeps the last-good value and never blanks.
      fetchCostRouting(key)
        .then((cr) => {
          if (alive) setCostRouting(cr);
        })
        .catch(() => undefined);
    };
    load();
    const id = window.setInterval(load, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [key]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Focus trap: focus the dialog on open, keep Tab cycling inside it, and
  // hand focus back to wherever the user was when it closes.
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialogRef.current?.focus();
    const trap = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const dlg = dialogRef.current;
      if (!dlg) return;
      const focusables = Array.from(
        dlg.querySelectorAll<HTMLElement>(
          'button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => !el.hasAttribute("disabled"));
      if (focusables.length === 0) {
        e.preventDefault();
        dlg.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables.at(-1);
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === dlg)) {
        e.preventDefault();
        last?.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", trap);
    return () => {
      document.removeEventListener("keydown", trap);
      opener?.focus();
    };
  }, []);

  const copyKey = useCallback(() => {
    navigator.clipboard
      ?.writeText(key)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1400);
      })
      .catch(() => undefined);
  }, [key]);

  // Live progress (from the /api/ws feed, threaded by the board) overrides the
  // snapshot fetched on open — but it's reconciled against the subtask list
  // below so a run with failed subtasks can never show a clean 100%.
  const overall = lp?.percent ?? proc?.progress?.overall_percent ?? null;
  const phase = lp?.phase ?? proc?.progress?.phase ?? wi.aifactory.phase ?? null;
  const subtask = lp?.subtask ?? proc?.progress?.current_subtask ?? null;

  const subStates = (proc?.subtasks ?? []).map((s) => stageState(s.status));
  const subDone = subStates.filter((s) => s === "done").length;
  const subFailed = subStates.filter((s) => s === "failed").length;
  const stageFailed = STAGES.some((s) => stageState(wi[s.key].status) === "failed");

  // Stage-level completion, keyed by the DAG's stage vocabulary (plan/code/test)
  // — the flow diagram tints its frame green when its stage is done, while its
  // per-node states stay honest below (#planflow-green).
  // #431: a TFactory task reports "done" once it has triaged, whether or not a
  // single lane executed — so a spec with 0 committed tests and 0/8 acceptance
  // criteria verified rendered "STAGE COMPLETE" to a reader looking at a
  // dashboard. The lane nodes now carry execution state, so require at least one
  // lane that actually ran before making the loudest claim on the page.
  //
  // When the graph has no nodes at all we fall back to the task status: absence
  // of a diagram is not evidence that nothing ran, and refusing to ever mark the
  // stage done would be its own dishonesty.
  const testLanes = proc?.graphs?.test?.nodes ?? [];
  const someLaneRan = testLanes.some((n) => n.status === "completed");
  const stageDone = {
    plan: stageState(wi.pfactory.status) === "done",
    code: stageState(wi.aifactory.status) === "done",
    test:
      stageState(wi.tfactory.status) === "done" &&
      (testLanes.length === 0 || someLaneRan),
  };

  const procAvailable = proc != null && proc.available !== false;

  // RFC-0015 §4 D2: show the traceability matrix once we have rows, OR — so a
  // reviewer of a verified task isn't left guessing — a "not available" note once
  // the test stage has engaged. A plan/code-only task that never reached verify
  // shows nothing, keeping the drawer uncluttered.
  const traceRows = proc?.traceability ?? null;
  const hasTraceRows = traceRows != null && traceRows.length > 0;
  const testEngaged = stageState(wi.tfactory.status) !== "idle";
  const showTraceability = hasTraceRows || testEngaged;

  return (
    <motion.div
      className="mc-term-overlay"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      onClick={() => {
        if (!reasonDirtyRef.current) onClose();
      }}
    >
      <motion.div
        className="mc-term-modal td-modal"
        initial={{ scale: 0.96, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        exit={{ scale: 0.96, opacity: 0 }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="td-title"
        tabIndex={-1}
        ref={dialogRef}
      >
        <div className="mc-term-modal-head">
          <span className="wi-key td-head-key" title={key}>#{keySlug(key)}</span>
          <button
            className={`td-copy ${copied ? "td-copy--ok" : ""}`}
            onClick={copyKey}
            title={`Copy full correlation key: ${key}`}
          >
            {copied ? "✓ copied" : "copy"}
          </button>
          <strong className="td-title" id="td-title">{displayTitle(wi.title, key)}</strong>
          <button className="mc-term-close" onClick={onClose} aria-label="Close task detail">
            ✕
          </button>
        </div>

        <div className="td-body">
          {/* Per-stage state */}
          <div className="td-stages">
            {STAGES.map((s) => {
              const st = wi[s.key];
              // Color the status by lifecycle state so a clean "done" reads green
              // at a glance (running=cyan, failed=red); planned/idle stay neutral.
              const cardState = stageState(st.status);
              return (
                <div className="td-stage" key={s.key}>
                  <div className="td-stage-h">
                    <span className="td-stage-label">{s.label}</span>
                    <span className="td-stage-svc">{s.svc}</span>
                  </div>
                  <div className={`td-stage-status td-stage-status--${cardState}`}>
                    {st.status || "—"}
                  </div>
                  {st.phase && <div className="td-stage-phase">{st.phase}</div>}
                  {st.extra?.access?.val3 === "not_run" && (
                    <div
                      className="td-stage-access"
                      title={st.extra.access.risk || ""}
                    >
                      {/* RFC-0007 #88: a credentialed (VAL-3) lane could not run —
                          surfaced honestly, never rendered as covered. */}
                      ⚠ VAL-3 not run —{" "}
                      {st.extra.access.reason || "access not available"}
                    </div>
                  )}
                  {st.extra?.verification?.achieved_level && (
                    <div
                      className={`td-stage-val td-stage-val--${
                        st.extra.verification.achieved_level === "VAL-3" ||
                        st.extra.verification.achieved_level === "VAL-4"
                          ? "high"
                          : "capped"
                      }`}
                      title={st.extra.verification.claim}
                    >
                      {/* RFC-0006 #76: render the assurance level + honest claim
                          so a VAL-2 result is never mistaken for "done". */}
                      {st.extra.verification.claim ||
                        `Verified to ${st.extra.verification.achieved_level}`}
                    </div>
                  )}
                  {/* #167: routing tier + security-gate verdicts + judge-vote
                      split for this stage. Renders nothing on old envelopes. */}
                  <StageGates extra={st.extra} />
                </div>
              );
            })}
          </div>

          {/* Live cost/usage stamp — renders nothing without worker data. With a
              Tier-1.5 heartbeat series it ticks smoothly; else stepwise. */}
          <LiveTaskStamp wi={wi} workers={workers ?? undefined} progress={progress ?? undefined} metered={metered} />

          {/* RFC-0014 (#124): pre-execution cost-aware routing — estimate vs
              actual spend, per-role/model cost, routing class + rationale,
              autonomy verdict + budget mode. Renders nothing without data. */}
          <CostRoutingPanel data={costRouting ?? undefined} />

          {/* RFC-0015 §3.3: readable spec/plan/tasks Markdown (the human-readable
              mirror PFactory emits) — rendered as docs, not raw structures.
              Renders nothing when the producer didn't emit the mirror. */}
          <ArtifactsPanel artifacts={proc?.artifacts ?? undefined} />

          {/* Live execution diagram — animated DAG with a plan/code/test stage
              switcher; defaults to the furthest stage (#94). */}
          <StageFlow
            graphs={proc?.graphs}
            fallback={proc?.graph}
            stageDone={stageDone}
            unreachable={proc?.unreachable}
          />

          {/* Test evidence — the browser-lane screenshots + recordings TFactory
              captured, proxied through CFactory so they render here on the
              finished task. Renders nothing when the task produced none. */}
          {proc?.evidence && (proc.evidence.screenshots.length > 0 || proc.evidence.videos.length > 0) && (
            <section className="td-section" data-testid="td-evidence">
              <h3>Test evidence</h3>
              {proc.evidence.videos.length > 0 && (
                <>
                  <div className="td-proc-meta"><b>Recordings ({proc.evidence.videos.length})</b></div>
                  <div className="td-evidence-grid">
                    {proc.evidence.videos.map((name) => (
                      <figure key={name} className="td-evidence-item">
                        <video
                          data-testid={`td-evidence-video-${name}`}
                          src={evidenceMediaUrl(key, "videos", name)}
                          controls
                        />
                        <figcaption title={name}>{name}</figcaption>
                      </figure>
                    ))}
                  </div>
                </>
              )}
              {proc.evidence.screenshots.length > 0 && (
                <>
                  <div className="td-proc-meta"><b>Screenshots ({proc.evidence.screenshots.length})</b></div>
                  <div className="td-evidence-grid">
                    {proc.evidence.screenshots.map((name) => (
                      <figure key={name} className="td-evidence-item">
                        <a href={evidenceMediaUrl(key, "screenshots", name)} target="_blank" rel="noreferrer">
                          <img
                            data-testid={`td-evidence-shot-${name}`}
                            src={evidenceMediaUrl(key, "screenshots", name)}
                            alt={name}
                            loading="lazy"
                          />
                        </a>
                        <figcaption title={name}>{name}</figcaption>
                      </figure>
                    ))}
                  </div>
                </>
              )}
            </section>
          )}

          {/* RFC-0015 §4 D2: requirement->test traceability matrix — AC x test x
              VAL x verdict. Shows the matrix when TFactory surfaced rows, else a
              "not available" note once the test stage has engaged; nothing on a
              task that never reached verify. */}
          {showTraceability && <TraceabilityPanel rows={traceRows} />}

          {/* Actions — approve / reject / unstick / remove */}
          <TaskActions
            wi={wi}
            onActed={onActed}
            onReasonDirty={(dirty) => {
              reasonDirtyRef.current = dirty;
            }}
          />

          {/* Live process — collapses to one muted line when unavailable */}
          {!procAvailable ? (
            <div className="td-empty">
              <b>Process</b> live detail unavailable{proc?.reason ? ` (${proc.reason})` : ""}
            </div>
          ) : (
            <section className="td-section">
              <h3>Process</h3>
              <div className="td-proc-meta">
                <span>phase: <b>{phase || "—"}</b></span>
                {subtask && <span>· {subtask}</span>}
                {proc?.progress?.message && <span className="mc-note">{proc.progress.message}</span>}
              </div>
              {subFailed > 0 ? (
                <SegmentedBar done={subDone} failed={subFailed} total={subStates.length} />
              ) : (
                <Bar pct={overall} label="Overall" failed={stageFailed} />
              )}
              {subFailed === 0 && <Bar pct={proc?.progress?.phase_percent} label="Phase" />}
              {proc?.subtasks && proc.subtasks.length > 0 && (
                <ul className="td-subtasks">
                  {proc.subtasks.map((s, i) => (
                    <li key={i} className={`td-sub td-sub--${(s.status || "").toLowerCase()}`}>
                      <span className="td-sub-dot" />
                      <span className="td-sub-title">{s.title || "subtask"}</span>
                      <span className="td-sub-status">{s.status || ""}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )}

          {/* Live terminal — only a full section when there's a live agent */}
          {agent ? (
            <section className="td-section">
              <h3>Live terminal</h3>
              <div className="td-term">
                <AgentTerminal agent={agent} fontSize={11} />
              </div>
            </section>
          ) : (
            <div className="td-empty">
              <b>Live terminal</b> no active session — this task isn’t running (or rmux is off)
            </div>
          )}

          {/* Timeline */}
          {wi.timeline.length === 0 ? (
            <div className="td-empty">
              <b>Timeline</b> no events yet
            </div>
          ) : (
            <section className="td-section">
              <h3>Timeline</h3>
              <ul className="td-timeline">
                {[...wi.timeline].reverse().map((e, i) => (
                  <li key={i}>
                    <span className={`td-tl-svc td-tl-svc--${e.service}`}>{e.service}</span>
                    <span className="td-tl-status">{e.status || "—"}</span>
                    {e.phase && <span className="td-tl-phase">{e.phase}</span>}
                    <span className="td-tl-time">{timeAgo(e.updated_at)}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      </motion.div>
    </motion.div>
  );
}
