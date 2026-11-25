// needsYou — pure classification of which work items are blocked on a human
// (#148). Kept separate from the view so the "what needs me and why" logic is
// unit-testable without a DOM. A work item needs attention when it's stalled
// (hung past its idle deadline) or parked in a stage awaiting a human decision
// (plan approval, code/verify review gate). Everything else — flowing or
// finished — is not in the inbox.
import { activeStage, isStalledStatus, overallState, stageState } from "./taskState";
import type { WorkItem } from "./api";
import { fmtAge } from "./format";

export type AttentionKind = "approval" | "review" | "stalled";
export type Stage = "plan" | "code" | "test";

export const OLDER_AGE_MS = 7 * 24 * 60 * 60 * 1000;
export const DISMISSED_STORAGE_KEY = "cfactory-needs-you-dismissed";

export interface Attention {
  wi: WorkItem;
  kind: AttentionKind;
  stage: Stage;
  service: string; // PFactory / AIFactory / TFactory
  reason: string;
  lastActivityAt: string | null;
  age: string;
  older: boolean;
}

const STAGE_BY_KEY: Record<
  "pfactory" | "aifactory" | "tfactory",
  { stage: Stage; service: string }
> = {
  pfactory: { stage: "plan", service: "PFactory" },
  aifactory: { stage: "code", service: "AIFactory" },
  tfactory: { stage: "test", service: "TFactory" },
};

/** Newest timeline event timestamp, else WorkItem.updated_at, else null. */
export function lastActivityTimestamp(wi: WorkItem): string | null {
  let newest: string | null = null;
  let newestMs = -Infinity;
  for (const ev of wi.timeline) {
    const ms = Date.parse(ev.updated_at);
    if (!Number.isNaN(ms) && ms > newestMs) {
      newestMs = ms;
      newest = ev.updated_at;
    }
  }
  if (newest) return newest;
  if (wi.updated_at && !Number.isNaN(Date.parse(wi.updated_at))) {
    return wi.updated_at;
  }
  return null;
}

/** Human-readable age for a work item's last activity, or "unknown age". */
export function lastActivityAge(
  wi: WorkItem,
  now: number = Date.now(),
): { lastActivityAt: string | null; age: string; older: boolean } {
  const ts = lastActivityTimestamp(wi);
  if (!ts) return { lastActivityAt: null, age: "unknown age", older: false };
  const ms = Date.parse(ts);
  if (Number.isNaN(ms)) return { lastActivityAt: null, age: "unknown age", older: false };
  const diffMs = Math.max(0, now - ms);
  const formatted = fmtAge(diffMs / 1000);
  return {
    lastActivityAt: ts,
    age: formatted || "0s",
    older: diffMs >= OLDER_AGE_MS,
  };
}

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
export function attentionFor(
  wi: WorkItem,
  options?: { now?: number | undefined },
): Attention | null {
  const now = options?.now ?? Date.now();
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

  const build = (
    kind: AttentionKind,
    stage: Stage,
    service: string,
    reason: string,
  ): Attention => {
    const { lastActivityAt, age, older } = lastActivityAge(wi, now);
    return {
      wi,
      kind,
      stage,
      service,
      reason,
      lastActivityAt,
      age,
      older,
    };
  };

  // Stalled: hung in a non-terminal stage past its idle deadline (#105). A stage
  // the upstream marked stalled reads "failed" overall but is still this case, so
  // the list agrees with the backend /api/needs-you/count (#479).
  const stalledKey = (["tfactory", "aifactory", "pfactory"] as const).find((k) =>
    isStalledStatus(statuses[k]),
  );
  if (wi.liveness?.stalled && (overall === "running" || stalledKey)) {
    const key = stalledKey ?? activeStage(statuses) ?? "aifactory";
    const meta = STAGE_BY_KEY[key];
    const age = fmtAge(wi.liveness.last_activity_age_seconds);
    return build(
      "stalled",
      meta.stage,
      meta.service,
      age
        ? `No movement for ${age} — past its idle deadline. The worker may be wedged.`
        : "Hung past its idle deadline. The worker may be wedged.",
    );
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
      return build(
        "approval",
        meta.stage,
        meta.service,
        gate ?? "Plan is awaiting your approval to emit and hand off.",
      );
    }
    if (key === "tfactory") {
      return build(
        "review",
        meta.stage,
        meta.service,
        gate ?? "Verified — awaiting your review before merge.",
      );
    }
    if (wi.aifactory.extra?.reviewReason === "needs_input") {
      return build("review", meta.stage, meta.service,
        wi.aifactory.extra.question?.question ?? "A question is waiting for your answer in AIFactory.");
    }
    if (wi.aifactory.extra?.reviewReason === "approval_rejected") {
      return build("review", meta.stage, meta.service, "Approval was rejected in AIFactory.");
    }
    return build("review", meta.stage, meta.service, gate ?? "Build is awaiting your review.");
  }

  return null;
}

const RANK: Record<AttentionKind, number> = { approval: 0, review: 1, stalled: 2 };

/** Everything that needs a human, most-actionable first (approvals, gates, stalls). */
export function attentionList(
  items: readonly WorkItem[],
  options?: { now?: number | undefined },
): Attention[] {
  return items
    .map((wi) => attentionFor(wi, options))
    .filter((a): a is Attention => a !== null)
    .sort((a, b) => RANK[a.kind] - RANK[b.kind]);
}

export function loadDismissedNeedsYou(): Set<string> {
  try {
    const raw = window.localStorage.getItem(DISMISSED_STORAGE_KEY);
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed)
      ? new Set(parsed.filter((k): k is string => typeof k === "string"))
      : new Set();
  } catch {
    return new Set();
  }
}

export function saveDismissedNeedsYou(keys: Iterable<string>): void {
  try {
    window.localStorage.setItem(DISMISSED_STORAGE_KEY, JSON.stringify([...keys]));
  } catch {
    /* localStorage unavailable */
  }
}
