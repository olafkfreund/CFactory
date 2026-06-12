import { Fragment, useMemo } from "react";
import { motion, useReducedMotion } from "framer-motion";
import type { WorkItem } from "./api";
import { activeStage, stageState } from "./taskState";

// The persistent PARR strip: the three-node pipeline lifted out of Mission
// Control into a slim full-width rail under the topbar, visible on every view.
// Clicking a node filters the Pipeline board to that stage; a stage with a
// failed task wears a red ring, and a fresh failure fires a one-shot flare.

export type StripStage = "pfactory" | "aifactory" | "tfactory";

export interface StripAlarm {
  stage: StripStage;
  tick: number; // monotonically increasing — keys the one-shot flare
}

const NODES: { key: StripStage; label: string; svc: string; cls: string }[] = [
  { key: "pfactory", label: "Plan", svc: "PFactory", cls: "plan" },
  { key: "aifactory", label: "Code", svc: "AIFactory", cls: "code" },
  { key: "tfactory", label: "Test", svc: "TFactory", cls: "test" },
];

/* Stage glyphs — the same drawings (and CSS animations) Mission Control used,
   now living on the strip. Each is drawn centred on (0,0). */
function DocGlyph() {
  return (
    <g className="mc-ic">
      <path className="mc-doc-page" d="M-11 -15 H4 L11 -8 V15 H-11 Z" />
      <path className="mc-doc-fold" d="M4 -15 V-8 H11" />
      <line className="mc-doc-l mc-doc-l1" x1={-6} y1={-3} x2={6} y2={-3} />
      <line className="mc-doc-l mc-doc-l2" x1={-6} y1={3} x2={6} y2={3} />
      <line className="mc-doc-l mc-doc-l3" x1={-6} y1={9} x2={2} y2={9} />
    </g>
  );
}

function BotGlyph() {
  return (
    <g className="mc-ic mc-bot mc-bot--live" transform="scale(1.05) translate(-16 -16)">
      <line className="mc-bot-antenna" x1={16} y1={3.5} x2={16} y2={7.5} />
      <circle className="mc-bot-led" cx={16} cy={2.6} r={1.7} />
      <rect className="mc-bot-head" x={5} y={7.5} width={22} height={17.5} rx={5} />
      <rect className="mc-bot-ear" x={2.4} y={13} width={2.6} height={6.5} rx={1.3} />
      <rect className="mc-bot-ear" x={27} y={13} width={2.6} height={6.5} rx={1.3} />
      <circle className="mc-bot-eye" cx={12} cy={15.5} r={2.2} />
      <circle className="mc-bot-eye" cx={20} cy={15.5} r={2.2} />
      <rect className="mc-bot-mouth" x={11} y={20.5} width={10} height={2.4} rx={1.2} />
    </g>
  );
}

function FlaskGlyph() {
  return (
    <g className="mc-ic">
      <path className="mc-flask-body" d="M-4 -15 V-3 L-11 12 Q-12 16 -8 16 H8 Q12 16 11 12 L4 -3 V-15" />
      <line className="mc-flask-lip" x1={-6.5} y1={-15} x2={6.5} y2={-15} />
      <path className="mc-flask-liquid" d="M-7.5 5 L-11 12 Q-12 16 -8 16 H8 Q12 16 11 12 L7.5 5 Z" />
      <circle className="mc-bub mc-bub1" cx={-2} cy={12} r={1.5} />
      <circle className="mc-bub mc-bub2" cx={3} cy={13} r={1.2} />
      <circle className="mc-bub mc-bub3" cx={0} cy={14} r={1} />
    </g>
  );
}

const GLYPH: Record<StripStage, () => React.JSX.Element> = {
  pfactory: DocGlyph,
  aifactory: BotGlyph,
  tfactory: FlaskGlyph,
};

export default function PipelineStrip({
  items,
  active,
  onSelect,
  alarm,
}: {
  items: WorkItem[];
  active: StripStage | null;
  onSelect: (stage: StripStage | null) => void;
  alarm: StripAlarm | null;
}) {
  const reduced = useReducedMotion();

  // Same semantics as the Mission Control ring: each item counts at the
  // furthest stage that is still active; failed work flags the stage it
  // failed at so trouble is visible from every view.
  const { counts, failed } = useMemo(() => {
    const counts: Record<StripStage, number> = { pfactory: 0, aifactory: 0, tfactory: 0 };
    const failed: Record<StripStage, number> = { pfactory: 0, aifactory: 0, tfactory: 0 };
    for (const wi of items) {
      const stage = activeStage({
        pfactory: wi.pfactory.status,
        aifactory: wi.aifactory.status,
        tfactory: wi.tfactory.status,
      });
      if (stage) counts[stage]++;
      for (const k of ["pfactory", "aifactory", "tfactory"] as const) {
        if (stageState(wi[k].status) === "failed") failed[k]++;
      }
    }
    return { counts, failed };
  }, [items]);

  return (
    <div className="pstrip" role="toolbar" aria-label="PARR pipeline — click a stage to filter the board">
      {NODES.map((n, i) => {
        const Glyph = GLYPH[n.key];
        const isOn = active === n.key;
        return (
          <Fragment key={n.key}>
            {i > 0 && (
              <span
                className={`pstrip-link pstrip-link--${NODES[i - 1].cls} ${counts[n.key] > 0 ? "flowing" : ""}`}
                aria-hidden="true"
              />
            )}
            <button
              className={`pstrip-node pstrip-node--${n.cls} ${isOn ? "is-on" : ""} ${failed[n.key] > 0 ? "has-fail" : ""}`}
              onClick={() => onSelect(isOn ? null : n.key)}
              aria-pressed={isOn}
              title={
                `${n.svc}: ${counts[n.key]} active` +
                (failed[n.key] > 0 ? `, ${failed[n.key]} failed` : "") +
                ` — click to ${isOn ? "clear the" : "filter the board to this"} stage`
              }
            >
              <span className="pstrip-ring">
                <svg className="pstrip-glyph" viewBox="-19 -19 38 38" aria-hidden="true">
                  <Glyph />
                </svg>
                {alarm && alarm.stage === n.key && !reduced && (
                  <motion.span
                    key={alarm.tick}
                    className="pstrip-flare"
                    initial={{ opacity: 0.9, scale: 1 }}
                    animate={{ opacity: 0, scale: 1.9 }}
                    transition={{ duration: 1.4, ease: "easeOut" }}
                    aria-hidden="true"
                  />
                )}
              </span>
              <span className="pstrip-meta">
                <span className="pstrip-label">{n.label}</span>
                <span className="pstrip-svc">{n.svc}</span>
              </span>
              <span className={`pstrip-count ${counts[n.key] === 0 ? "pstrip-count--zero" : ""}`}>
                {counts[n.key]}
              </span>
              {failed[n.key] > 0 && (
                <span className="pstrip-fail" title={`${failed[n.key]} task(s) failed at ${n.label}`}>
                  {failed[n.key]} failed
                </span>
              )}
            </button>
          </Fragment>
        );
      })}
      <span className="pstrip-tail">
        {active ? (
          <button className="pstrip-clear" onClick={() => onSelect(null)}>
            ✕ clear {NODES.find((n) => n.key === active)?.label.toLowerCase()} filter
          </button>
        ) : (
          <span className="pstrip-hint">PARR pipeline</span>
        )}
      </span>
    </div>
  );
}
