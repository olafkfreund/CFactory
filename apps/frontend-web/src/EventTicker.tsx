import { useMemo } from "react";
import { motion } from "framer-motion";
import type { WorkItem } from "./api";
import { keySlug } from "./correlationKey";

// The live event ticker rail: a right-hand collapsible column that streams the
// merged factory timeline (the same WorkItem data the WS feed keeps fresh, so
// it ticks in real time). Mono, colour-coded by service. Failures get pinned
// at the top until dismissed. Hidden below ~1100px via CSS.

export interface TickerPin {
  id: number;
  key: string;
  title: string;
  at: number; // epoch ms
}

interface Entry {
  service: string;
  key: string;
  status: string | null;
  phase: string | null;
  at: string;
}

const SVC_SHORT: Record<string, string> = {
  pfactory: "PLAN",
  aifactory: "CODE",
  tfactory: "TEST",
};

function hhmm(at: string | number): string {
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return "--:--";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function EventTicker({
  items,
  pins,
  onUnpin,
  open,
  onToggle,
}: {
  items: WorkItem[];
  pins: TickerPin[];
  onUnpin: (id: number) => void;
  open: boolean;
  onToggle: () => void;
}) {
  const entries = useMemo(() => {
    const out: Entry[] = [];
    for (const wi of items) {
      for (const e of wi.timeline) {
        out.push({
          service: e.service,
          key: wi.correlation_key,
          status: e.status,
          phase: e.phase,
          at: e.updated_at,
        });
      }
    }
    out.sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime());
    return out.slice(0, 60);
  }, [items]);

  if (!open) {
    return (
      <aside className="ticker ticker--closed" aria-label="Live event feed (collapsed)">
        <button className="ticker-toggle" onClick={onToggle} title="Open the event feed" aria-expanded={false}>
          ◂
        </button>
        <span className="ticker-vlabel">EVENT FEED</span>
        {pins.length > 0 && <span className="ticker-closed-alert" title={`${pins.length} pinned failure(s)`}>{pins.length}</span>}
      </aside>
    );
  }

  return (
    <aside className="ticker" aria-label="Live event feed">
      <div className="ticker-head">
        <span className="ticker-title">EVENT FEED</span>
        <span className="ticker-dot" aria-hidden="true" />
        <button className="ticker-toggle" onClick={onToggle} title="Collapse the event feed" aria-expanded>
          ▸
        </button>
      </div>

      {pins.length > 0 && (
        <div className="ticker-pins" role="alert">
          {pins.map((p) => (
            <div className="ticker-pin" key={p.id}>
              <div className="ticker-pin-body">
                <span className="ticker-pin-k">TASK FAILED · {hhmm(p.at)}</span>
                <span className="ticker-pin-t" title={p.key}>
                  {keySlug(p.key)}
                </span>
              </div>
              <button className="ticker-pin-x" onClick={() => onUnpin(p.id)} aria-label="Dismiss pinned failure">
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="ticker-body">
        {entries.length === 0 ? (
          <p className="ticker-empty">Waiting for factory events…</p>
        ) : (
          entries.map((e) => (
            <motion.div
              className={`tick tick--${e.service}`}
              key={`${e.key}-${e.service}-${e.at}`}
              initial={{ opacity: 0, x: 8 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.25 }}
              title={`#${e.key} — ${e.service} ${e.status ?? ""}${e.phase ? ` (${e.phase})` : ""}`}
            >
              <span className="tick-time">{hhmm(e.at)}</span>
              <span className="tick-svc">{SVC_SHORT[e.service] ?? e.service.slice(0, 4).toUpperCase()}</span>
              <span className="tick-main">
                <span className="tick-key">{keySlug(e.key)}</span>{" "}
                <span className="tick-status">{e.status ?? "—"}</span>
                {e.phase && <span className="tick-phase"> · {e.phase}</span>}
              </span>
            </motion.div>
          ))
        )}
      </div>
    </aside>
  );
}
