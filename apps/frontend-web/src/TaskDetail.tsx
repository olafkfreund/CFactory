import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import {
  fetchLiveAgents,
  fetchProcess,
  type LiveAgent,
  type LiveProgress,
  type ProcessDetail,
  type WorkItem,
} from "./api";
import { AgentTerminal } from "./LiveAgents";
import TaskActions from "./TaskActions";

const STAGES = [
  { key: "pfactory", label: "Plan", svc: "PFactory" },
  { key: "aifactory", label: "Code", svc: "AIFactory" },
  { key: "tfactory", label: "Test", svc: "TFactory" },
] as const;

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function Bar({ pct, label }: { pct: number | null | undefined; label: string }) {
  const v = typeof pct === "number" ? Math.max(0, Math.min(100, pct)) : null;
  return (
    <div className="td-bar">
      <div className="td-bar-label">
        <span>{label}</span>
        <span>{v == null ? "—" : `${Math.round(v)}%`}</span>
      </div>
      <div className="td-bar-track">
        <div className="td-bar-fill" style={{ width: `${v ?? 0}%` }} />
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
  lp?: LiveProgress;
  onClose: () => void;
  onActed?: () => void;
}) {
  const [proc, setProc] = useState<ProcessDetail | null>(null);
  const [agent, setAgent] = useState<LiveAgent | null>(null);
  const key = wi.correlation_key;

  useEffect(() => {
    let alive = true;
    fetchProcess(key)
      .then((p) => alive && setProc(p))
      .catch(() => alive && setProc({ available: false, correlation_key: key }));
    fetchLiveAgents()
      .then((r) => alive && setAgent(r.agents.find((a) => a.correlation_key === key) ?? null))
      .catch(() => alive && setAgent(null));
    return () => {
      alive = false;
    };
  }, [key]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Live progress (from the /api/ws feed, threaded by the board) overrides the
  // snapshot fetched on open.
  const overall = lp?.percent ?? proc?.progress?.overall_percent ?? null;
  const phase = lp?.phase ?? proc?.progress?.phase ?? wi.aifactory.phase ?? null;
  const subtask = lp?.subtask ?? proc?.progress?.current_subtask ?? null;

  return (
    <motion.div
      className="mc-term-overlay"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      onClick={onClose}
    >
      <motion.div
        className="mc-term-modal td-modal"
        initial={{ scale: 0.96, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        exit={{ scale: 0.96, opacity: 0 }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mc-term-modal-head">
          <span className="wi-key">#{key}</span>
          <strong className="td-title">{wi.title || "Untitled work item"}</strong>
          <button className="mc-term-close" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="td-body">
          {/* Per-stage state */}
          <div className="td-stages">
            {STAGES.map((s) => {
              const st = wi[s.key];
              return (
                <div className="td-stage" key={s.key}>
                  <div className="td-stage-h">
                    <span className="td-stage-label">{s.label}</span>
                    <span className="td-stage-svc">{s.svc}</span>
                  </div>
                  <div className="td-stage-status">{st.status || "—"}</div>
                  {st.phase && <div className="td-stage-phase">{st.phase}</div>}
                </div>
              );
            })}
          </div>

          {/* Actions — approve / reject / unstick / remove */}
          <TaskActions wi={wi} onActed={onActed} />

          {/* Live process */}
          <section className="td-section">
            <h3>Process</h3>
            {proc && !proc.available ? (
              <p className="mc-note">
                Live process detail unavailable{proc.reason ? ` (${proc.reason})` : ""}.
              </p>
            ) : (
              <>
                <div className="td-proc-meta">
                  <span>phase: <b>{phase || "—"}</b></span>
                  {subtask && <span>· {subtask}</span>}
                  {proc?.progress?.message && <span className="mc-note">{proc.progress.message}</span>}
                </div>
                <Bar pct={overall} label="Overall" />
                <Bar pct={proc?.progress?.phase_percent} label="Phase" />
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
              </>
            )}
          </section>

          {/* Live terminal */}
          <section className="td-section">
            <h3>Live terminal</h3>
            {agent ? (
              <div className="td-term">
                <AgentTerminal agent={agent} fontSize={11} />
              </div>
            ) : (
              <p className="mc-note">No active terminal — this task isn’t running (or rmux is off).</p>
            )}
          </section>

          {/* Timeline */}
          <section className="td-section">
            <h3>Timeline</h3>
            {wi.timeline.length === 0 ? (
              <p className="mc-note">No events yet.</p>
            ) : (
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
            )}
          </section>
        </div>
      </motion.div>
    </motion.div>
  );
}
