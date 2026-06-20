// Transient notification toasts for the cockpit, extracted from App.tsx (#115).
// Failure toasts persist until dismissed; the rest auto-clear. Pure extraction —
// behaviour-preserving, no logic change.
import { useEffect } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { EVENT_LABEL, type EventKind } from "./notify";
import { keySlug } from "./correlationKey";
import type { ToastEntry } from "./useDashboard";

const TOAST_KIND_CLASS: Record<EventKind, string> = {
  new: "toast--new",
  done: "toast--done",
  failed: "toast--failed",
  review: "toast--review",
};

export function Toasts({
  toasts,
  onDismiss,
}: {
  toasts: ToastEntry[];
  onDismiss: (id: number) => void;
}) {
  return (
    <div className="toast-stack" role="status" aria-live="polite">
      <AnimatePresence initial={false}>
        {toasts.map((t) => (
          <Toast key={t.id} entry={t} onDismiss={onDismiss} />
        ))}
      </AnimatePresence>
    </div>
  );
}

function Toast({ entry, onDismiss }: { entry: ToastEntry; onDismiss: (id: number) => void }) {
  // Failure toasts persist until the user dismisses them; the rest auto-clear.
  const persistent = entry.ev.kind === "failed";
  useEffect(() => {
    if (persistent) return;
    const id = window.setTimeout(() => onDismiss(entry.id), 7000);
    return () => window.clearTimeout(id);
  }, [entry.id, onDismiss, persistent]);
  return (
    <motion.div
      className={`toast ${TOAST_KIND_CLASS[entry.ev.kind]}`}
      initial={{ opacity: 0, x: 24 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 24, transition: { duration: 0.2 } }}
      onClick={() => onDismiss(entry.id)}
      role="button"
      tabIndex={0}
    >
      <span className="toast-dot" />
      <div className="toast-body">
        <div className="toast-title">
          {EVENT_LABEL[entry.ev.kind]}{" "}
          <span className="toast-key" title={entry.ev.key}>
            #{keySlug(entry.ev.key)}
          </span>
        </div>
        <div className="toast-detail">{entry.ev.title}</div>
      </div>
      {persistent && (
        <button
          className="toast-x"
          aria-label="Dismiss failure notification"
          onClick={(e) => {
            e.stopPropagation();
            onDismiss(entry.id);
          }}
        >
          ✕
        </button>
      )}
    </motion.div>
  );
}
