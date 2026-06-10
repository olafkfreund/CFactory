// Browser/OS notification plumbing. The in-app toast (see Toasts in App.tsx) is
// always shown; an OS-level Notification is fired too when the user has granted
// permission, so a backgrounded/minimised cockpit still alerts them.

export type EventKind = "new" | "done" | "failed" | "review";

export interface TaskEvent {
  key: string;
  kind: EventKind;
  title: string; // human label for the work item
}

export const EVENT_LABEL: Record<EventKind, string> = {
  new: "New task started",
  done: "Task finished",
  failed: "Task failed",
  review: "Awaiting review",
};

/** Ask once for OS-notification permission. Safe to call when unsupported. */
export function ensureNotifyPermission(): void {
  if (typeof Notification === "undefined") return;
  if (Notification.permission === "default") {
    try {
      void Notification.requestPermission();
    } catch {
      /* older browsers throw on the promise form — ignore */
    }
  }
}

/** Fire an OS notification if permitted. The in-app toast is handled separately. */
export function osNotify(ev: TaskEvent): void {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try {
    new Notification(`${EVENT_LABEL[ev.kind]} · #${ev.key}`, {
      body: ev.title,
      tag: `${ev.kind}:${ev.key}`, // collapse duplicates for the same task/kind
      icon: "/favicon.ico",
    });
  } catch {
    /* construction can throw on some platforms — non-fatal */
  }
}
