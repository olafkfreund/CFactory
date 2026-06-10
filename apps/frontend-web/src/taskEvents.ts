// Turn a stream of WorkItem updates into notification-worthy events by diffing
// each item's overall state against what we last saw. The caller keeps the
// state map across updates and seeds it on first load WITHOUT notifying, so a
// fresh page never dumps a notification per existing task.

import type { WorkItem } from "./api";
import { overallState, stageState, type TaskState } from "./taskState";
import type { EventKind, TaskEvent } from "./notify";

export function stateOf(wi: WorkItem): TaskState {
  return overallState([
    stageState(wi.pfactory.status),
    stageState(wi.aifactory.status),
    stageState(wi.tfactory.status),
  ]);
}

// Which terminal/attention states are worth a notification on transition.
const TRANSITION_KIND: Partial<Record<TaskState, EventKind>> = {
  done: "done",
  failed: "failed",
  review: "review",
};

/**
 * Diff `items` against `prev` (key → last seen state), mutating `prev` to the
 * new states. Returns events to notify on. When `seeded` is false we only seed
 * the map and return nothing (the initial hydrate is not "news").
 */
export function diffEvents(
  prev: Map<string, TaskState>,
  items: WorkItem[],
  seeded: boolean,
): TaskEvent[] {
  const events: TaskEvent[] = [];
  for (const wi of items) {
    const cur = stateOf(wi);
    const was = prev.get(wi.correlation_key);
    prev.set(wi.correlation_key, cur);
    if (!seeded || cur === was) continue;

    const label = wi.title || wi.correlation_key;
    if (was === undefined) {
      // Newly seen mid-session = a task entered the pipeline. Skip if it shows
      // up already finished/idle (backfill, not real news).
      if (cur !== "idle" && cur !== "done" && cur !== "failed") {
        events.push({ key: wi.correlation_key, kind: "new", title: label });
      }
      continue;
    }
    const kind = TRANSITION_KIND[cur];
    if (kind) events.push({ key: wi.correlation_key, kind, title: label });
  }
  return events;
}
