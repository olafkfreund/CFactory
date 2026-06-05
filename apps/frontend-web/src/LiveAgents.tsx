import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { fetchLiveAgents, openAgentConsole, type LiveAgent } from "./api";

// xterm theme matched to the cockpit's Gruvbox palette (see index.css :root).
const TERM_THEME = {
  background: "#32302f",
  foreground: "#ebdbb2",
  cursor: "#32302f", // hidden — this view is read-only
  black: "#282828",
  brightBlack: "#7c6f64",
  red: "#fb4934",
  green: "#b8bb26",
  yellow: "#fabd2f",
  blue: "#83a598",
  magenta: "#d3869b",
  cyan: "#8ec07c",
  white: "#ebdbb2",
} as const;

type Phase = { loading: boolean; rmuxEnabled: boolean; agents: LiveAgent[] };

/** One agent's live, read-only terminal. Streams ANSI bytes from the backend
 *  proxy into an xterm instance; disposes both on unmount. */
function AgentTerminal({ agent, fontSize }: { agent: LiveAgent; fontSize: number }) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = host.current;
    if (!el) return;

    const term = new Terminal({
      disableStdin: true,
      cursorBlink: false,
      convertEol: false,
      fontSize,
      fontFamily: '"JetBrains Mono", ui-monospace, monospace',
      scrollback: 2000,
      theme: TERM_THEME,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(el);
    const refit = () => {
      try {
        fit.fit();
      } catch {
        /* element not laid out yet */
      }
    };
    refit();

    const ws = openAgentConsole(agent.ws_path);
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") term.write(ev.data);
      else term.write(new Uint8Array(ev.data as ArrayBuffer));
    };
    ws.onclose = () => term.write("\r\n\x1b[2m— stream ended —\x1b[0m\r\n");

    window.addEventListener("resize", refit);
    return () => {
      window.removeEventListener("resize", refit);
      ws.close();
      term.dispose();
    };
  }, [agent.ws_path, fontSize]);

  return <div className="mc-term" ref={host} />;
}

function AgentTile({ agent, onExpand }: { agent: LiveAgent; onExpand: () => void }) {
  return (
    <div className="mc-agent-card">
      <button className="mc-agent-head" onClick={onExpand} title="Expand">
        <span className="mc-agent-dot mc-agent-dot--live" />
        <span className="mc-agent-key">#{agent.correlation_key}</span>
        {agent.phase && <span className="mc-agent-sub">{agent.phase}</span>}
        <span className="mc-agent-expand">⤢</span>
      </button>
      <AgentTerminal agent={agent} fontSize={9} />
    </div>
  );
}

export default function LiveAgents({ reloadSignal }: { reloadSignal: number }) {
  const [phase, setPhase] = useState<Phase>({ loading: true, rmuxEnabled: false, agents: [] });
  const [expanded, setExpanded] = useState<LiveAgent | null>(null);

  useEffect(() => {
    let alive = true;
    fetchLiveAgents()
      .then((r) => {
        if (alive) setPhase({ loading: false, rmuxEnabled: r.rmux_enabled, agents: r.agents });
      })
      .catch(() => {
        if (alive) setPhase({ loading: false, rmuxEnabled: false, agents: [] });
      });
    return () => {
      alive = false;
    };
  }, [reloadSignal]);

  return (
    <div className="mc-panel">
      <h2 className="panel-title">Live agents</h2>
      <Body phase={phase} onExpand={setExpanded} />

      <AnimatePresence>
        {expanded && (
          <motion.div
            className="mc-term-overlay"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => setExpanded(null)}
          >
            <motion.div
              className="mc-term-modal"
              initial={{ scale: 0.96, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.96, opacity: 0 }}
              onClick={(e) => e.stopPropagation()}
            >
              <div className="mc-term-modal-head">
                <span className="mc-agent-dot mc-agent-dot--live" />
                <strong>#{expanded.correlation_key}</strong>
                {expanded.title && <span className="mc-agent-sub">{expanded.title}</span>}
                {expanded.phase && <span className="mc-agent-sub">· {expanded.phase}</span>}
                <button className="mc-term-close" onClick={() => setExpanded(null)}>
                  ✕
                </button>
              </div>
              <AgentTerminal agent={expanded} fontSize={13} />
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function Body({ phase, onExpand }: { phase: Phase; onExpand: (a: LiveAgent) => void }) {
  if (phase.loading) {
    return (
      <div className="mc-agents">
        {[0, 1, 2].map((i) => (
          <div className="mc-agent-ph" key={i}>
            <span className="mc-agent-dot" />
            connecting…
          </div>
        ))}
      </div>
    );
  }
  if (!phase.rmuxEnabled) {
    return <p className="mc-note">Live agents are off — AIFactory’s rmux console is disabled.</p>;
  }
  if (phase.agents.length === 0) {
    return <p className="mc-note">No agents running right now. Terminals appear here as tasks start.</p>;
  }
  return (
    <div className="mc-agents">
      {phase.agents.map((a) => (
        <AgentTile key={a.correlation_key} agent={a} onExpand={() => onExpand(a)} />
      ))}
    </div>
  );
}
