import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import CommandPalette, { buildPaletteRows, type PaletteCommand } from "./CommandPalette";

// Command palette (#147). Rendered to static markup (react-dom/server, no jsdom —
// matches the other frontend tests). We assert the closed/open contract and that
// commands render grouped. Federated task results (#149) arrive via a debounced
// /api/search effect, which react-dom/server does not run, so the task lane is
// covered by the backend search tests rather than here.
const cmds: PaletteCommand[] = [
  { id: "nav-overview", group: "Go to", label: "Mission Control", run: () => {} },
  { id: "act-refresh", group: "Actions", label: "Refresh data", hint: "R", run: () => {} },
];

const projects = [
  { slug: "alpha", name: "Alpha App" },
  { slug: "beta", name: "Beta" },
];

describe("CommandPalette", () => {
  it("renders nothing when closed", () => {
    const html = renderToStaticMarkup(
      <CommandPalette
        open={false}
        onClose={() => {}}
        commands={cmds}
        onOpenTask={() => {}}
      />,
    );
    expect(html).toBe("");
  });

  it("shows commands grouped when open, and no tasks without a query", () => {
    const html = renderToStaticMarkup(
      <CommandPalette
        open
        onClose={() => {}}
        commands={cmds}
        onOpenTask={() => {}}
      />,
    );
    expect(html).toContain("Command palette");
    expect(html).toContain("Mission Control");
    expect(html).toContain("Go to");
    expect(html).toContain("Actions");
    // tasks are query-gated → the Tasks group must not appear on a bare open
    expect(html).not.toContain("Tasks");
    // the first row is selected by default
    expect(html).toContain('aria-selected="true"');
  });

  it("includes Switch project... when onSwitchProject is provided and enters project mode", () => {
    const html = renderToStaticMarkup(
      <CommandPalette
        open
        onClose={() => {}}
        commands={cmds}
        onOpenTask={() => {}}
        projects={projects}
        onSwitchProject={() => {}}
      />,
    );
    expect(html).toContain("Switch project...");

    const onEnterProjectMode = vi.fn();
    const normalRows = buildPaletteRows({
      q: "switch",
      commands: cmds,
      results: [],
      projectMode: false,
      projects,
      onEnterProjectMode,
    });
    expect(normalRows).toHaveLength(1);
    const switchRow = normalRows[0];
    expect(switchRow?.kind).toBe("cmd");
    if (switchRow?.kind === "cmd") {
      expect(switchRow.keepOpen).toBe(true);
      switchRow.cmd.run();
    }
    expect(onEnterProjectMode).toHaveBeenCalledTimes(1);
  });

  it("renders All projects at the top of project mode and filters projects by query", () => {
    const html = renderToStaticMarkup(
      <CommandPalette
        open
        onClose={() => {}}
        commands={cmds}
        onOpenTask={() => {}}
        projects={projects}
        onSwitchProject={() => {}}
        initialProjectMode
      />,
    );
    expect(html).toContain("Projects");
    expect(html).toContain("All projects");
    expect(html).toContain("Alpha App");
    expect(html).toContain("Beta");

    const projectRows = buildPaletteRows({
      q: "",
      commands: cmds,
      results: [],
      projectMode: true,
      projects,
    });
    expect(projectRows.map((r) => (r.kind === "project" ? r.slug : "other"))).toEqual([
      null,
      "alpha",
      "beta",
    ]);

    const filteredRows = buildPaletteRows({
      q: "beta",
      commands: cmds,
      results: [],
      projectMode: true,
      projects,
    });
    expect(filteredRows).toEqual([
      {
        kind: "project",
        id: "proj-beta",
        group: "Projects",
        slug: "beta",
        label: "Beta",
        hint: "beta",
      },
    ]);
  });
});
