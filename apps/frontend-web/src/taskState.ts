// Shared task-lifecycle classification for the cockpit. The factories speak
// different status vocabularies (AIFactory: backlog/in_progress/ai_review/
// human_review/done; PFactory board_state; TFactory triaged/passed/…), so we
// map any status string to one canonical state. Used by the Running tasks view
// and live-agent gating so a parked/queued task is never shown as "running".

export type TaskState = "running" | "review" | "queued" | "done" | "failed" | "idle";

const FAIL = /(fail|reject|error|abort|cancel|block|discard)/i;
const DONE =
  /(^|[_-])(merged|done|complete|completed|passed|approved|qa[_-]?approved|shipped|succeeded|success|ready|emitted|triaged|closed|accepted)([_-]|$)/i;
const REVIEW = /(human[_-]?review|ai[_-]?review|needs[_-]?review|in[_-]?review|awaiting|review)/i;
const QUEUED = /(backlog|pending|queued|todo|icebox|draft|not[_-]?started)/i;
const RUNNING =
  /(in[_-]?progress|running|executing|working|coding|planning|generating|building|act)/i;

/** Classify one stage's status string into a canonical lifecycle state. */
export function stageState(status?: string | null): TaskState {
  if (!status) return "idle";
  if (FAIL.test(status)) return "failed";
  if (DONE.test(status)) return "done";
  if (REVIEW.test(status)) return "review";
  if (QUEUED.test(status)) return "queued";
  if (RUNNING.test(status)) return "running";
  return "running"; // unknown but present → assume engaged
}

/** Roll the three stage states into one overall state for a work item.
 *  Priority: failed > running > review > done > queued. */
export function overallState(states: TaskState[]): TaskState {
  const engaged = states.filter((s) => s !== "idle");
  if (engaged.length === 0) return "idle";
  if (engaged.includes("failed")) return "failed";
  if (engaged.includes("running")) return "running";
  if (engaged.includes("review")) return "review";
  if (engaged.includes("done")) return "done";
  return "queued";
}

export const STATE_LABEL: Record<TaskState, string> = {
  running: "running",
  review: "in review",
  queued: "queued",
  done: "done",
  failed: "failed",
  idle: "idle",
};

// Pill style class (reuses .status-pill.* in index.css; "review"/"queued" added).
export const STATE_PILL: Record<TaskState, string> = {
  running: "run",
  review: "review",
  queued: "queued",
  done: "ok",
  failed: "fail",
  idle: "queued",
};
