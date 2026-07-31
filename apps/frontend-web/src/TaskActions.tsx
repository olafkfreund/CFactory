import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  proposeAction,
  executeAction,
  detailOf,
  approvalBlockReason,
  type PreparedAction,
  type ExecuteResult,
  type WorkItem,
} from "./api";
import { activeStage, overallState, stageState, STATE_LABEL } from "./taskState";

// The action set the cockpit can drive on an in-flight / stuck task. Each maps
// to a backend propose_* tool, which builds the real upstream call(s); nothing
// is written until the confirm step. Destructive kinds are flagged so the
// confirm dialog can warn. Buttons are gated by the task's canonical lifecycle
// state (taskState.ts) so you can't approve something that isn't in review.
type Kind = "approve_review" | "reject_review" | "recover" | "delete_task";

const BUTTONS: { kind: Kind; label: string; short: string; cls: string; needsReason?: boolean; danger?: boolean }[] = [
  { kind: "approve_review", label: "Approve (PR + merge)", short: "Approve", cls: "act--approve" },
  { kind: "reject_review", label: "Reject (send back)", short: "Reject", cls: "act--reject", needsReason: true },
  { kind: "recover", label: "Unstick", short: "Unstick", cls: "act--recover" },
  { kind: "delete_task", label: "Remove", short: "Remove", cls: "act--delete", danger: true },
];

type Stage = "idle" | "reason" | "proposing" | "confirm" | "executing" | "done";

export default function TaskActions({
  wi,
  onActed,
  onReasonDirty,
}: {
  wi: WorkItem;
  onActed?: () => void;
  /** Fires with true while the reject-reason textarea has text — the parent
   *  modal uses it to refuse destructive overlay-click dismissal. */
  onReasonDirty?: (dirty: boolean) => void;
}) {
  const [pending, setPending] = useState<Kind | null>(null);
  const [stage, setStage] = useState<Stage>("idle");
  const [reason, setReason] = useState("");
  const [action, setAction] = useState<PreparedAction | null>(null);
  const [result, setResult] = useState<ExecuteResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    onReasonDirty?.(stage === "reason" && reason.trim().length > 0);
  }, [stage, reason, onReasonDirty]);

  const states = [wi.pfactory, wi.aifactory, wi.tfactory].map((s) => stageState(s.status));
  const overall = overallState(states);
  // The stage an action would target (furthest active) — matches the backend's
  // _review_target. PFactory plan sessions have no upstream delete.
  const active = activeStage({
    pfactory: wi.pfactory.status,
    aifactory: wi.aifactory.status,
    tfactory: wi.tfactory.status,
  });

  /** Why a button is unavailable in the task's current state, or null when ok. */
  const gate = (kind: Kind): string | null => {
    switch (kind) {
      case "approve_review": {
        if (overall !== "review")
          return `needs a task in review — this one is ${STATE_LABEL[overall]}`;
        // #245: a gate-blocked plan CANNOT be approved — PFactory answers 409.
        // Say so here instead of letting the user click, wait, and read the
        // refusal. Reject stays enabled: sending it back is the way out.
        return approvalBlockReason(wi.pfactory.extra?.review);
      }
      case "reject_review":
        return overall === "review"
          ? null
          : `needs a task in review — this one is ${STATE_LABEL[overall]}`;
      case "recover":
        return overall === "failed"
          ? null
          : `only for stuck or failed tasks — this one is ${STATE_LABEL[overall]}`;
      case "delete_task":
        return active === "pfactory"
          ? "PFactory plans can't be deleted — use Reject to send it back"
          : null; // code/test: always available (confirm step guards it)
    }
  };

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
  const busyLock = stage !== "idle" && stage !== "done";

  // Group identical disabled-reasons so the subtext reads once, not four times.
  const whyGroups = (() => {
    const m = new Map<string, string[]>();
    for (const b of BUTTONS) {
      const why = gate(b.kind);
      if (!why) continue;
      const labels = m.get(why) ?? [];
      labels.push(b.short);
      m.set(why, labels);
    }
    return [...m.entries()];
  })();

  return (
    <section className="td-section">
      <h3>Actions</h3>
      <p className="mc-note">Act on this task in its factory. Writes ask for confirmation first.</p>

      <div className="td-actions">
        {BUTTONS.map((b) => {
          const why = gate(b.kind);
          return (
            <button
              key={b.kind}
              className={`act-btn ${b.cls}`}
              disabled={busyLock}
              aria-disabled={why ? true : undefined}
              title={why ?? undefined}
              onClick={() => {
                if (why || busyLock) return;
                if (stage === "done") reset();
                start(b.kind, b.needsReason);
              }}
            >
              {b.label}
            </button>
          );
        })}
      </div>

      {whyGroups.length > 0 && (
        <p className="act-why">
          {whyGroups.map(([why, labels]) => `${labels.join(" / ")}: ${why}`).join(" · ")}
        </p>
      )}

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

      {stage === "done" && result && <ActionResult result={result} onClose={reset} />}
    </section>
  );
}

// The finished-action banner. Exported so the sentence a refusal renders is
// testable on its own — the banner sits behind internal `stage === "done"`
// state that a static-markup render (this app has no jsdom) cannot reach.
export function ActionResult({
  result,
  onClose,
}: {
  result: ExecuteResult;
  onClose: () => void;
}) {
  // Why it failed: the backend's own `error` when it set one (transport or
  // validation), otherwise the refusing service's `{detail}` from the body.
  const why = result.ok ? null : (result.error ?? detailOf(result.body));
  return (
    <div className={`act-result ${result.ok ? "act-result--ok" : "act-result--fail"}`}>
      {result.ok ? "✓ Done." : `✗ Failed (HTTP ${result.status_code})`}
      {why && <span> — {why}</span>}
      {result.steps && result.steps.some((s) => !s.ok) && (
        <span> — {result.steps.filter((s) => !s.ok).map((s) => s.endpoint).join(", ")}</span>
      )}
      <button className="act-btn act--cancel" onClick={onClose}>
        Close
      </button>
    </div>
  );
}
