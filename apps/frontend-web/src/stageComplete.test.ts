import { describe, expect, it } from "vitest";

import { testStageIsComplete } from "./stageComplete";

const lane = (id: string, status: string | null) =>
  ({ id, label: id, kind: id, status, deps: [] }) as never;

describe("testStageIsComplete", () => {
  it("is not complete when a lane failed", () => {
    // Card #561: STAGE COMPLETE beside "1 done, 1 failed". Unit (0/7 run)
    // failed, Browser (4/4) passed, and one executed lane satisfied the
    // first version of this check.
    expect(
      testStageIsComplete(true, [lane("unit", "failed"), lane("browser", "completed")]),
    ).toBe(false);
  });

  it("is not complete when no lane executed", () => {
    // Spec 155: "Browser (8/8) STAGE COMPLETE" with 0 committed tests.
    expect(testStageIsComplete(true, [lane("browser", null)])).toBe(false);
  });

  it("is complete when a lane ran and none failed", () => {
    expect(testStageIsComplete(true, [lane("browser", "completed")])).toBe(true);
  });

  it("falls back to the task status when there is no diagram", () => {
    // Absence of a diagram is not evidence that nothing ran.
    expect(testStageIsComplete(true, [])).toBe(true);
  });

  it("is never complete while the task itself is not done", () => {
    expect(testStageIsComplete(false, [lane("browser", "completed")])).toBe(false);
  });

  it("a failed lane outranks several passing ones", () => {
    expect(
      testStageIsComplete(true, [
        lane("unit", "completed"),
        lane("api", "completed"),
        lane("browser", "failed"),
      ]),
    ).toBe(false);
  });
});
