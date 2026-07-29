import { describe, it, expect } from "vitest";
import { isActiveState, overallState, type TaskState } from "./taskState";

// The Running-tasks view spells its Active chip as isActiveState(overall).
// These pin the meaning of "active" itself, which used to be `!== "done"`.
describe("what counts as active", () => {
  it("excludes failed, so a reaped orphan leaves the Active tab", () => {
    // AIFactory#1064's reaper marks dead tasks `cancelled`; stageState maps
    // that to "failed". If Active still counted it, reaping would change the
    // card's wording and nothing else -- the original complaint was that dead
    // tasks keep showing as active.
    expect(isActiveState("failed")).toBe(false);
  });

  it("excludes done and never-started", () => {
    expect(isActiveState("done")).toBe(false);
    expect(isActiveState("idle")).toBe(false);
  });

  it("includes work that is genuinely outstanding", () => {
    for (const s of ["running", "review", "queued"] as TaskState[]) {
      expect(isActiveState(s), s).toBe(true);
    }
  });

  it("a task whose stage failed is not active", () => {
    // End to end through the real roll-up, not just the predicate: a cancelled
    // AIFactory stage must produce a non-active overall state.
    const overall = overallState(["done", "failed", "idle"]);
    expect(overall).toBe("failed");
    expect(isActiveState(overall)).toBe(false);
  });

  it("no state is both active and failed", () => {
    // The bug was double-counting: every failure appeared under Active AND
    // Failed, inflating the number an operator reads first.
    const states: TaskState[] = ["running", "review", "queued", "done", "failed", "idle"];
    for (const s of states) {
      expect(isActiveState(s) && s === "failed", s).toBe(false);
    }
  });
});
