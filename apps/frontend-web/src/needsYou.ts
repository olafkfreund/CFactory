// needsYou — pure classification of which work items are blocked on a human
// (#148). Kept separate from the view so the "what needs me and why" logic is
// unit-testable without a DOM. A work item needs attention when it's stalled
// (hung past its idle deadline) or parked in a stage awaiting a human decision
// (plan approval, code/verify review gate). Everything else — flowing or
// finished — is not in the inbox.
import { activeStage, overallState, stageState } from "./taskState";
import type { WorkItem } from "./api";
import { fmtAge } from "./format";

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

/**
 * A gate-specific reason when a stage was parked by a machine security gate
 * (#167): the injection scan flagged the task, or the dependency review failed.
 * The inbox card must say WHICH gate fired, not just "needs review". Null when
 * neither gate verdict is present (the generic review reason then applies).
 */
function gateReason(state: WorkItem["pfactory"]): string | null {
  const scan = state.extra?.injection_scan;
  if (scan?.verdict === "flagged") {
    return `Flagged by the prompt-injection scan${scan.reason ? ` — ${scan.reason}` : ""}. Review before it proceeds.`;
  }
  const dep = state.extra?.dependency_review;
  if (dep?.status === "fail") {
    const n = dep.findings?.length ?? 0;
    return `Failed the dependency review${n > 0 ? ` — ${String(n)} finding${n === 1 ? "" : "s"}` : ""}. Review before it proceeds.`;
  }
  return null;
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
    // A machine security gate (#167) is more specific than the generic review
    // copy — the card must name the gate that fired.
    const gate = gateReason(wi[key]);
    if (key === "pfactory") {
      return {
        wi,
        kind: "approval",
        stage: meta.stage,
        service: meta.service,
        reason: gate ?? "Plan is awaiting your approval to emit and hand off.",
      };
    }
    if (key === "tfactory") {
      return {
        wi,
        kind: "review",
        stage: meta.stage,
        service: meta.service,
        reason: gate ?? "Verified — awaiting your review before merge.",
      };
    }
    return {
      wi,
      kind: "review",
      stage: meta.stage,
      service: meta.service,
      reason: gate ?? "Build is awaiting your review.",
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
