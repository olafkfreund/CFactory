import { describe, expect, it } from "vitest";
import { stageState } from "./taskState";

describe("stageState", () => {
  it("reads a stalled or stuck stage as failed, never running (#479)", () => {
    expect(stageState("stalled")).toBe("failed");
    expect(stageState("watchdog_stalled")).toBe("failed");
    expect(stageState("stuck")).toBe("failed");
  });

  it("does not read a substring like installed as stalled", () => {
    expect(stageState("deps_installed")).toBe("running");
  });
});
