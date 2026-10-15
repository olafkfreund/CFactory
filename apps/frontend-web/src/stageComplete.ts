import type { ProcessGraph } from "./api";

type LaneNode = ProcessGraph["nodes"][number];

/**
 * Whether the test stage may be shown as COMPLETE (#431).
 *
 * Extracted from TaskDetail because this decision has been wrong twice, in
 * opposite directions, and inline logic in a component cannot be tested.
 *
 * A TFactory task reports "done" once it has TRIAGED, whether or not a lane
 * executed. Spec 155 rendered "Browser (8/8) STAGE COMPLETE" with 0 committed
 * tests and 0/8 acceptance criteria verified.
 *
 * The first fix required one executed lane, which left the same false green in
 * a different shape: card #561 rendered STAGE COMPLETE beside "1 done, 1
 * failed" -- Unit (0/7 run) failed, Browser (4/4) passed, and one executed lane
 * satisfied the check. A stage with a failed lane is not complete under any
 * reading, and this is the loudest claim on the page.
 *
 * With no nodes at all it falls back to the task status: absence of a diagram is
 * not evidence that nothing ran, and refusing to ever mark a stage done would be
 * its own dishonesty.
 */
export function testStageIsComplete(taskDone: boolean, lanes: LaneNode[]): boolean {
  if (!taskDone) return false;
  if (lanes.some((n) => n.status === "failed")) return false;
  if (lanes.length === 0) return true;
  return lanes.some((n) => n.status === "completed");
}
