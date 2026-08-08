import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import TaskFlow, { StageFlow } from "./TaskFlow";
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

// #249: an upstream fetch failure used to be indistinguishable from "that stage
// does not exist", so the DAG fell back to an earlier stage and reported it as the
// state of the run. This is the screenshot-equivalent assertion: we render the
// exact payload the live cockpit received while AIFactory was unreachable mid-build
// and assert on the emitted markup — first the wrong answer it used to give, then
// the unknown it gives now.
describe("StageFlow with an unreachable stage (#249)", () => {
  const planGraph: ProcessGraph = {
    stage: "plan",
    nodes: [
      { id: "c1", label: "child 1", status: "completed" },
      { id: "c2", label: "child 2", status: "completed", deps: ["c1"] },
    ],
  };
  const codeGraph: ProcessGraph = {
    stage: "code",
    nodes: [{ id: "s1", label: "Wire the route", status: "in_progress" }],
  };
  const stageDone = { plan: true, code: false, test: false };

  // THE DEFECT, reproduced: this is exactly what the component was handed on the
  // polls where the code fetch failed — plan only, no `unreachable`. It renders a
  // completed plan as the answer, with no way back to the code stage.
  it("without the marker it still downgrades — a completed plan while the build runs", () => {
    const html = renderToStaticMarkup(
      <StageFlow graphs={{ plan: planGraph }} stageDone={stageDone} />,
    );
    expect(html).toContain("Plan flow");
    expect(html).toContain("stage complete"); // the wrong answer that looks right
    expect(html).not.toContain("tf-switch"); // single stage → switcher vanishes
  });

  it("renders the unreachable stage as unknown instead of falling back", () => {
    const html = renderToStaticMarkup(
      <StageFlow graphs={{ plan: planGraph }} stageDone={stageDone} unreachable={["code"]} />,
    );
    expect(html).toContain("tf-unknown-code");
    expect(html).toContain("Code stage unknown");
    // The plan's "stage complete" must NOT be what the operator is shown.
    expect(html).not.toContain("stage complete");
    // …and the switcher survives, so the plan DAG is still one click away.
    expect(html).toContain("tf-switch");
    expect(html).toContain("tf-switch-tab--unknown");
  });

  // Mutation check, other direction: the marker must not manufacture doubt about a
  // stage that DID answer. Code present + reachable → the code DAG, no unknown.
  it("renders the real graph when the stage is reachable", () => {
    const html = renderToStaticMarkup(
      <StageFlow graphs={{ plan: planGraph, code: codeGraph }} stageDone={stageDone} />,
    );
    expect(html).toContain("Code flow");
    expect(html).not.toContain("tf-unknown-code");
    expect(html).not.toContain("stage unknown");
  });

  // Mutation check: an unreachable stage that DOES have a graph (fetched on an
  // earlier poll, cached upstream) still renders its graph — `unreachable` only
  // speaks for stages we have nothing for.
  it("prefers a real graph over the unknown panel", () => {
    const html = renderToStaticMarkup(
      <StageFlow
        graphs={{ plan: planGraph, code: codeGraph }}
        stageDone={stageDone}
        unreachable={["code"]}
      />,
    );
    expect(html).toContain("Code flow");
    expect(html).not.toContain("tf-unknown-code");
  });

  // Factory#431: the wire type is open, so a stage name this build has never heard
  // of is dropped at the render site rather than inventing a tab for it.
  it("ignores an unknown stage name", () => {
    const html = renderToStaticMarkup(
      <StageFlow graphs={{ plan: planGraph }} stageDone={stageDone} unreachable={["deploy"]} />,
    );
    expect(html).toContain("Plan flow");
    expect(html).not.toContain("stage unknown");
  });
});
