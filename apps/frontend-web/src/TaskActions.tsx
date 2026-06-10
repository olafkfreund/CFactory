import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  proposeAction,
  executeAction,
  type PreparedAction,
  type ExecuteResult,
  type WorkItem,
} from "./api";

// The action set the cockpit can drive on an in-flight / stuck task. Each maps
// to a backend propose_* tool, which builds the real upstream call(s); nothing
// is written until the confirm step. Destructive kinds are flagged so the
// confirm dialog can warn.
type Kind = "approve_review" | "reject_review" | "recover" | "delete_task";

const BUTTONS: { kind: Kind; label: string; cls: string; needsReason?: boolean; danger?: boolean }[] = [
  { kind: "approve_review", label: "Approve (PR + merge)", cls: "act--approve" },
  { kind: "reject_review", label: "Reject (send back)", cls: "act--reject", needsReason: true },
  { kind: "recover", label: "Unstick", cls: "act--recover" },
  { kind: "delete_task", label: "Remove", cls: "act--delete", danger: true },
];

type Stage = "idle" | "reason" | "proposing" | "confirm" | "executing" | "done";

export default function TaskActions({ wi, onActed }: { wi: WorkItem; onActed?: () => void }) {
  const [pending, setPending] = useState<Kind | null>(null);
  const [stage, setStage] = useState<Stage>("idle");
  const [reason, setReason] = useState("");
  const [action, setAction] = useState<PreparedAction | null>(null);
  const [result, setResult] = useState<ExecuteResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setPending(null);
    setStage("idle");
    setReason("");
    setAction(null);
    setResult(null);
    setError(null);
  };

  const start = (kind: Kind, needsReason?: boolean) => {
    setPending(kind);
    setError(null);
    if (needsReason) {
      setStage("reason");
    } else {
      void propose(kind);
    }
  };

  const propose = async (kind: Kind, note?: string) => {
    setStage("proposing");
    try {
      const a = await proposeAction(kind, wi.correlation_key, note);
      setAction(a);
      setStage("confirm");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStage("idle");
    }
  };

  const confirm = async () => {
    if (!action) return;
    setStage("executing");
    try {
      const r = await executeAction(action);
      setResult(r);
      setStage("done");
      if (r.ok) onActed?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStage("confirm");
    }
  };

  const meta = BUTTONS.find((b) => b.kind === pending);

  return (
    <section className="td-section">
      <h3>Actions</h3>
      <p className="mc-note">Act on this task in its factory. Writes ask for confirmation first.</p>

      <div className="td-actions">
        {BUTTONS.map((b) => (
          <button
            key={b.kind}
            className={`act-btn ${b.cls}`}
            disabled={stage !== "idle" && stage !== "done"}
            onClick={() => {
              if (stage === "done") reset();
              start(b.kind, b.needsReason);
            }}
          >
            {b.label}
          </button>
        ))}
      </div>

      {error && <p className="act-error">⚠ {error}</p>}

      <AnimatePresence>
        {stage !== "idle" && stage !== "done" && (
          <motion.div
            className="act-panel"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
          >
            {stage === "reason" && (
              <div className="act-reason">
                <label htmlFor="act-reason-in">Why are you rejecting this? (sent to the fixer)</label>
                <textarea
                  id="act-reason-in"
                  rows={3}
                  value={reason}
                  autoFocus
                  placeholder="e.g. missing tests for the error path"
                  onChange={(e) => setReason(e.target.value)}
                />
                <div className="act-row">
                  <button className="act-btn act--cancel" onClick={reset}>
                    Cancel
                  </button>
                  <button
                    className="act-btn act--reject"
                    disabled={!reason.trim()}
                    onClick={() => void propose("reject_review", reason)}
                  >
                    Continue
                  </button>
                </div>
              </div>
            )}

            {stage === "proposing" && <p className="mc-note">Preparing…</p>}

            {stage === "confirm" && action && (
              <div className="act-confirm">
                <p className="act-rationale">{action.rationale}</p>
                <div className="act-calls">
                  <span className="act-calls-h">Will call {action.target_service}:</span>
                  <code>
                    {action.method} {action.endpoint}
                  </code>
                  {action.follow_ups?.map((s, i) => (
                    <code key={i}>
                      {s.method} {s.endpoint}
                    </code>
                  ))}
                </div>
                <div className="act-row">
                  <button className="act-btn act--cancel" onClick={reset}>
                    Cancel
                  </button>
                  <button className={`act-btn ${meta?.danger ? "act--delete" : "act--approve"}`} onClick={() => void confirm()}>
                    Confirm
                  </button>
                </div>
              </div>
            )}

            {stage === "executing" && <p className="mc-note">Executing…</p>}
          </motion.div>
        )}
      </AnimatePresence>

      {stage === "done" && result && (
        <div className={`act-result ${result.ok ? "act-result--ok" : "act-result--fail"}`}>
          {result.ok ? "✓ Done." : `✗ Failed (HTTP ${result.status_code})`}
          {!result.ok && result.error && <span> — {result.error}</span>}
          {result.steps && result.steps.some((s) => !s.ok) && (
            <span> — {result.steps.filter((s) => !s.ok).map((s) => s.endpoint).join(", ")}</span>
          )}
          <button className="act-btn act--cancel" onClick={reset}>
            Close
          </button>
        </div>
      )}
    </section>
  );
}
