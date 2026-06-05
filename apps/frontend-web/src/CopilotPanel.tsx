import { useCallback, useEffect, useRef, useState } from "react";
import {
  askCopilot,
  fetchAnomalies,
  fetchRollups,
  type Anomaly,
  type Rollups,
} from "./api";

interface Msg {
  role: "you" | "copilot";
  text: string;
}

export default function CopilotPanel({ reloadSignal }: { reloadSignal: number }) {
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);
  const [rollups, setRollups] = useState<Rollups | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [asking, setAsking] = useState(false);
  const threadRef = useRef<HTMLDivElement>(null);

  const loadInsights = useCallback(() => {
    fetchAnomalies().then(setAnomalies).catch(() => setAnomalies([]));
    fetchRollups().then(setRollups).catch(() => setRollups(null));
  }, []);

  useEffect(() => {
    loadInsights();
  }, [loadInsights, reloadSignal]);

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
          anomalies.map((a, i) => (
            <div className={`card card--${a.severity}`} key={`${a.correlation_key}-${a.kind}-${i}`}>
              <div className="card-head">
                <span className="card-kind">{a.kind}</span>
                <span className="card-key">#{a.correlation_key}</span>
              </div>
              <div className="card-detail">{a.detail}</div>
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
