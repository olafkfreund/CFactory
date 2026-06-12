import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { fetchLiveAgents, openAgentConsole, type LiveAgent } from "./api";
import { keySlug } from "./correlationKey";

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
export function AgentTerminal({
  agent,
  fontSize,
  onEnded,
}: {
  agent: LiveAgent;
  fontSize: number;
  onEnded?: () => void;
}) {
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

    let gotData = false;
    const ws = openAgentConsole(agent.ws_path);
    ws.onmessage = (ev) => {
      gotData = true;
      if (typeof ev.data === "string") term.write(ev.data);
      else term.write(new Uint8Array(ev.data as ArrayBuffer));
    };
    ws.onclose = () => {
      // No bytes before close → there's no live pane for this task (it finished
      // or never started). Say so plainly rather than the cryptic "stream ended".
      term.write(
        gotData
          ? "\r\n\x1b[2m— agent finished —\x1b[0m\r\n"
          : "\r\n\x1b[2m— no live session (agent not running) —\x1b[0m\r\n",
      );
      onEnded?.();
    };

    window.addEventListener("resize", refit);
    return () => {
      window.removeEventListener("resize", refit);
      ws.close();
      term.dispose();
    };
  }, [agent.ws_path, fontSize, onEnded]);

  return <div className="mc-term" ref={host} />;
}

/** Animated robot-head avatar. Eyes blink and the antenna LED pulses while the
 *  agent is live; all motion is pure CSS keyed off the `mc-bot--live` class. */
function RobotHead({ live = true }: { live?: boolean }) {
  return (
    <svg
      className={`mc-bot${live ? " mc-bot--live" : ""}`}
      viewBox="0 0 32 32"
      width="30"
      height="30"
      aria-hidden="true"
    >
      <line className="mc-bot-antenna" x1="16" y1="3.5" x2="16" y2="7.5" />
      <circle className="mc-bot-led" cx="16" cy="2.6" r="1.7" />
      <rect className="mc-bot-head" x="5" y="7.5" width="22" height="17.5" rx="5" />
      <rect className="mc-bot-ear" x="2.4" y="13" width="2.6" height="6.5" rx="1.3" />
      <rect className="mc-bot-ear" x="27" y="13" width="2.6" height="6.5" rx="1.3" />
      <circle className="mc-bot-eye" cx="12" cy="15.5" r="2.2" />
      <circle className="mc-bot-eye" cx="20" cy="15.5" r="2.2" />
      <rect className="mc-bot-mouth" x="11" y="20.5" width="10" height="2.4" rx="1.2" />
    </svg>
  );
}

/** Small terminal/console glyph — the affordance for opening the live console. */
function ConsoleIcon() {
  return (
    <svg className="mc-console-icn" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
      <rect x="2.5" y="4.5" width="19" height="15" rx="2.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="M6 10l3 2.5-3 2.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      <line x1="12.5" y1="15" x2="17" y2="15" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

function AgentTile({ agent, onExpand }: { agent: LiveAgent; onExpand: () => void }) {
  return (
    <button className="la-card" onClick={onExpand} title={`Open rmux console — #${agent.correlation_key}`}>
      <RobotHead live />
      <span className="la-bubble">
        <span className="la-id">#{keySlug(agent.correlation_key)}</span>
        {agent.phase && <span className="la-phase">{agent.phase}</span>}
      </span>
      <span className="la-console" aria-label="Open rmux console">
        <ConsoleIcon />
      </span>
    </button>
  );
}

export default function LiveAgents({ reloadSignal }: { reloadSignal: number }) {
  const [phase, setPhase] = useState<Phase>({ loading: true, rmuxEnabled: false, agents: [] });
  const [expanded, setExpanded] = useState<LiveAgent | null>(null);

  useEffect(() => {
    let alive = true;
    const poll = () =>
      fetchLiveAgents()
        .then((r) => {
          if (alive) setPhase({ loading: false, rmuxEnabled: r.rmux_enabled, agents: r.agents });
        })
        .catch(() => {
          if (alive) setPhase((p) => ({ ...p, loading: false }));
        });
    poll();
    // Auto-refresh so agents appear when tasks start and disappear when they
    // finish — no manual reload needed.
    const id = window.setInterval(poll, 5000);
    return () => {
      alive = false;
      window.clearInterval(id);
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
                <strong title={expanded.correlation_key}>#{keySlug(expanded.correlation_key)}</strong>
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
