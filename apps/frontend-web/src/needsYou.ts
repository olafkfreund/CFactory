// needsYou — pure classification of which work items are blocked on a human
// (#148). Kept separate from the view so the "what needs me and why" logic is
// unit-testable without a DOM. A work item needs attention when it's stalled
// (hung past its idle deadline) or parked in a stage awaiting a human decision
// (plan approval, code/verify review gate). Everything else — flowing or
// finished — is not in the inbox.
import { activeStage, overallState, stageState } from "./taskState";
import type { WorkItem } from "./api";

export type AttentionKind = "approval" | "review" | "stalled";
export type Stage = "plan" | "code" | "test";

export interface Attention {
  wi: WorkItem;
  kind: AttentionKind;
  stage: Stage;
  service: string; // PFactory / AIFactory / TFactory
  reason: string;
}

const STAGE_BY_KEY: Record<
  "pfactory" | "aifactory" | "tfactory",
  { stage: Stage; service: string }
> = {
  pfactory: { stage: "plan", service: "PFactory" },
  aifactory: { stage: "code", service: "AIFactory" },
  tfactory: { stage: "test", service: "TFactory" },
};

/** Compact "3m" / "2h" / "1d" from a second count, for the reason line. */
export function fmtAge(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "";
  if (sec < 90) return `${String(Math.round(sec))}s`;
  const m = sec / 60;
  if (m < 90) return `${String(Math.round(m))}m`;
  const h = m / 60;
  if (h < 36) return `${String(Math.round(h))}h`;
  return `${String(Math.round(h / 24))}d`;
}

/**
 * The single most pressing reason this item needs a human, or null. Stalled wins
 * (it's the unhealthy case); otherwise the furthest stage sitting in review — a
 * PFactory review is a plan approval, a downstream review is a merge/verify gate.
 */
export function attentionFor(wi: WorkItem): Attention | null {
  const statuses = {
    pfactory: wi.pfactory.status,
    aifactory: wi.aifactory.status,
    tfactory: wi.tfactory.status,
  };
  const overall = overallState([
    stageState(statuses.pfactory),
    stageState(statuses.aifactory),
    stageState(statuses.tfactory),
  ]);

  // Stalled: hung in a non-terminal stage past its idle deadline (#105).
  if (wi.liveness?.stalled && overall === "running") {
    const key = activeStage(statuses) ?? "aifactory";
    const meta = STAGE_BY_KEY[key];
    const age = fmtAge(wi.liveness.last_activity_age_seconds);
    return {
      wi,
      kind: "stalled",
      stage: meta.stage,
      service: meta.service,
      reason: age
        ? `No movement for ${age} — past its idle deadline. The worker may be wedged.`
        : "Hung past its idle deadline. The worker may be wedged.",
    };
  }

  // Furthest stage parked for a human decision (test → code → plan).
  const order: (keyof typeof STAGE_BY_KEY)[] = ["tfactory", "aifactory", "pfactory"];
  for (const key of order) {
    if (stageState(statuses[key]) !== "review") continue;
    const meta = STAGE_BY_KEY[key];
    if (key === "pfactory") {
      return {
        wi,
        kind: "approval",
        stage: meta.stage,
        service: meta.service,
        reason: "Plan is awaiting your approval to emit and hand off.",
      };
    }
    if (key === "tfactory") {
      return {
        wi,
        kind: "review",
        stage: meta.stage,
        service: meta.service,
        reason: "Verified — awaiting your review before merge.",
      };
    }
    return {
      wi,
      kind: "review",
      stage: meta.stage,
      service: meta.service,
      reason: "Build is awaiting your review.",
    };
  }

  return null;
}

const RANK: Record<AttentionKind, number> = { approval: 0, review: 1, stalled: 2 };

/** Everything that needs a human, most-actionable first (approvals, gates, stalls). */
export function attentionList(items: readonly WorkItem[]): Attention[] {
  return items
    .map(attentionFor)
    .filter((a): a is Attention => a !== null)
    .sort((a, b) => RANK[a.kind] - RANK[b.kind]);
}
