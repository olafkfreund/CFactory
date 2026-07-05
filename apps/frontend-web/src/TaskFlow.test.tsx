import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import TaskFlow from "./TaskFlow";
import type { ProcessGraph } from "./api";

// The "stage complete" green cue (#planflow-green). A stage finishing cleanly is
// a stage-level fact (the work-item status), so it drives a frame + flag on the
// whole diagram — while the per-node states stay honest (a planned-but-unverified
// AC node is never painted done). We render to static markup and assert on the
// HTML, matching the zero-jsdom posture of the other frontend tests.
describe("TaskFlow stage-complete cue", () => {
  const graph: ProcessGraph = {
    stage: "plan",
    nodes: [
      { id: "ac1", label: "AC1", status: "planned" },
      { id: "ac2", label: "AC2", status: "planned", deps: ["ac1"] },
    ],
  } as ProcessGraph;

  it("tints the frame and shows the flag when the stage is done", () => {
    const html = renderToStaticMarkup(<TaskFlow graph={graph} stageDone />);
    expect(html).toContain("tf--stage-done");
    expect(html).toContain("stage complete");
  });

  it("stays neutral when the stage is not done, and never paints planned nodes done", () => {
    const html = renderToStaticMarkup(<TaskFlow graph={graph} stageDone={false} />);
    expect(html).not.toContain("tf--stage-done");
    expect(html).not.toContain("stage complete");
    // per-node honesty: planned nodes render pending, not done, regardless of stage.
    expect(html).toContain("tf-node--pending");
    expect(html).not.toContain("tf-node--done");
  });

  it("keeps nodes honest even when the stage is marked done", () => {
    const html = renderToStaticMarkup(<TaskFlow graph={graph} stageDone />);
    // frame says "stage complete" but the planned AC nodes are still pending.
    expect(html).toContain("tf-node--pending");
    expect(html).not.toContain("tf-node--done");
  });
});
