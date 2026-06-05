import { useCallback, useEffect, useRef, useState } from "react";
import {
  askCopilot,
  executeAction,
  fetchAnomalies,
  fetchAudit,
  fetchRollups,
  proposeAction,
  type Anomaly,
  type AuditEntry,
  type PreparedAction,
  type Rollups,
} from "./api";

interface Msg {
  role: "you" | "copilot";
  text: string;
}

// Which action a given anomaly kind proposes. Advise + confirm: clicking only
// PROPOSES; nothing executes until the human confirms.
const ACTION_FOR_KIND: Record<string, string> = {
  handback_loop: "kick_handback",
  failure: "kick_handback",
  stuck: "trigger_handoff",
};

export default function CopilotPanel({ reloadSignal }: { reloadSignal: number }) {
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);
  const [rollups, setRollups] = useState<Rollups | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [asking, setAsking] = useState(false);
  // Per-card pending PreparedAction awaiting confirmation, keyed by card index.
  const [pending, setPending] = useState<Record<number, PreparedAction>>({});
  // Cards currently proposing or executing, keyed by card index.
  const [busy, setBusy] = useState<Record<number, boolean>>({});
  const threadRef = useRef<HTMLDivElement>(null);

  const loadAudit = useCallback(() => {
    fetchAudit().then(setAudit).catch(() => setAudit([]));
  }, []);

  const loadInsights = useCallback(() => {
    fetchAnomalies().then(setAnomalies).catch(() => setAnomalies([]));
    fetchRollups().then(setRollups).catch(() => setRollups(null));
    loadAudit();
  }, [loadAudit]);

  useEffect(() => {
    loadInsights();
  }, [loadInsights, reloadSignal]);

  const onPropose = useCallback(async (idx: number, kind: string, correlationKey: string) => {
    const actionKind = ACTION_FOR_KIND[kind];
    if (!actionKind) return;
    setBusy((b) => ({ ...b, [idx]: true }));
    try {
      const action = await proposeAction(actionKind, correlationKey);
      setPending((p) => ({ ...p, [idx]: action }));
    } catch {
      /* surface nothing destructive — proposing is read-only */
    } finally {
      setBusy((b) => ({ ...b, [idx]: false }));
    }
  }, []);

  const onCancel = useCallback((idx: number) => {
    setPending((p) => {
      const next = { ...p };
      delete next[idx];
      return next;
    });
  }, []);

  const onConfirm = useCallback(async (idx: number, action: PreparedAction) => {
    setBusy((b) => ({ ...b, [idx]: true }));
    try {
      await executeAction(action);
    } finally {
      setBusy((b) => ({ ...b, [idx]: false }));
      onCancel(idx);
      loadInsights();
    }
  }, [onCancel, loadInsights]);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages]);

  const send = useCallback(async () => {
    const q = input.trim();
    if (!q || asking) return;
    setMessages((m) => [...m, { role: "you", text: q }]);
    setInput("");
    setAsking(true);
    try {
      const answer = await askCopilot(q);
      setMessages((m) => [...m, { role: "copilot", text: answer }]);
    } catch (err) {
      setMessages((m) => [
        ...m,
        { role: "copilot", text: `⚠️ ${err instanceof Error ? err.message : String(err)}` },
      ]);
    } finally {
      setAsking(false);
    }
  }, [input, asking]);

  return (
    <section className="copilot">
      <div className="insights">
        <h2 className="panel-title">Insights</h2>
        {rollups && (
          <div className="card card--rollup">
            <strong>{rollups.total_work_items}</strong> work items ·
            plan {rollups.by_stage.plan} / code {rollups.by_stage.code} / test {rollups.by_stage.test} ·
            {" "}{rollups.total_events} events
          </div>
        )}
        {anomalies.length === 0 ? (
          <div className="card card--ok">No anomalies 🎉</div>
        ) : (
          anomalies.map((a, i) => {
            const actionKind = ACTION_FOR_KIND[a.kind];
            const prepared = pending[i];
            const isBusy = busy[i] ?? false;
            return (
              <div className={`card card--${a.severity}`} key={`${a.correlation_key}-${a.kind}-${i}`}>
                <div className="card-head">
                  <span className="card-kind">{a.kind}</span>
                  <span className="card-key">#{a.correlation_key}</span>
                </div>
                <div className="card-detail">{a.detail}</div>
                {actionKind && !prepared && (
                  <div className="card-actions">
                    <button
                      className="btn btn--sm"
                      onClick={() => onPropose(i, a.kind, a.correlation_key)}
                      disabled={isBusy}
                    >
                      {isBusy ? "…" : `Propose ${actionKind.replace("_", " ")}`}
                    </button>
                  </div>
                )}
                {prepared && (
                  <div className="confirm">
                    <div className="confirm-rationale">{prepared.rationale}</div>
                    <div className="confirm-target">
                      <span className="card-kind">{prepared.method}</span>{" "}
                      <code>{prepared.target_service}{prepared.endpoint}</code>
                    </div>
                    <div className="card-actions">
                      <button
                        className="btn btn--sm"
                        onClick={() => onConfirm(i, prepared)}
                        disabled={isBusy}
                      >
                        {isBusy ? "…" : "Confirm"}
                      </button>
                      <button
                        className="btn btn--sm btn--ghost"
                        onClick={() => onCancel(i)}
                        disabled={isBusy}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })
        )}

        <h2 className="panel-title">Recent actions (audit)</h2>
        {audit.length === 0 ? (
          <div className="card card--rollup">No actions yet</div>
        ) : (
          audit.map((e) => (
            <div className="audit-row" key={e.id}>
              <span className="card-kind">{e.kind}</span>
              <span className="card-key">#{e.correlation_key}</span>
              <span className={e.ok ? "audit-ok" : "audit-fail"}>
                {e.ok ? "ok" : "fail"} {e.status_code}
              </span>
              <span className="audit-ts">{new Date(e.ts).toLocaleTimeString()}</span>
            </div>
          ))
        )}
      </div>

      <div className="chat">
        <h2 className="panel-title">Copilot</h2>
        <div className="chat-thread" ref={threadRef}>
          {messages.length === 0 && (
            <p className="chat-hint">Ask e.g. “where is #142 and why is it stuck?”</p>
          )}
          {messages.map((m, i) => (
            <div className={`bubble bubble--${m.role}`} key={i}>
              <span className="bubble-role">{m.role}</span>
              {m.text}
            </div>
          ))}
          {asking && <div className="bubble bubble--copilot">…thinking</div>}
        </div>
        <div className="chat-input">
          <input
            value={input}
            placeholder="Ask the copilot…"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
            disabled={asking}
          />
          <button className="btn" onClick={send} disabled={asking || !input.trim()}>
            Ask
          </button>
        </div>
      </div>
    </section>
  );
}
