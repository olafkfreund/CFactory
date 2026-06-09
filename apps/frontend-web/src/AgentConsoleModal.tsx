import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AgentTerminal } from "./LiveAgents";
import type { LiveAgent } from "./api";

/** Full-screen-ish modal hosting one agent's read-only live rmux terminal.
 *  Reused by the Live agents panel and the Running tasks view. When the upstream
 *  stream closes (agent finished / no live pane), `onEnded` fires so the caller
 *  can drop the tile instead of leaving a dead "— stream ended —" console. */
export default function AgentConsoleModal({
  agent,
  onClose,
  onEnded,
}: {
  agent: LiveAgent;
  onClose: () => void;
  onEnded?: (key: string) => void;
}) {
  const [ended, setEnded] = useState(false);
  return (
    <AnimatePresence>
      <motion.div
        className="mc-term-overlay"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
      >
        <motion.div
          className="mc-term-modal"
          initial={{ scale: 0.96, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.96, opacity: 0 }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mc-term-modal-head">
            <span className={`mc-agent-dot ${ended ? "" : "mc-agent-dot--live"}`} />
            <strong>#{agent.correlation_key}</strong>
            {agent.title && <span className="mc-agent-sub">{agent.title}</span>}
            {agent.phase && <span className="mc-agent-sub">· {agent.phase}</span>}
            {ended && <span className="mc-agent-sub mc-agent-sub--ended">agent finished</span>}
            <button className="mc-term-close" onClick={onClose}>
              ✕
            </button>
          </div>
          <AgentTerminal
            agent={agent}
            fontSize={13}
            onEnded={() => {
              setEnded(true);
              onEnded?.(agent.correlation_key);
            }}
          />
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
