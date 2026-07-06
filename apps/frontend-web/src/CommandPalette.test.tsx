import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import CommandPalette, { type PaletteCommand } from "./CommandPalette";

// Command palette (#147). Rendered to static markup (react-dom/server, no jsdom —
// matches the other frontend tests). We assert the closed/open contract and that
// commands render grouped. Federated task results (#149) arrive via a debounced
// /api/search effect, which react-dom/server does not run, so the task lane is
// covered by the backend search tests rather than here.
const cmds: PaletteCommand[] = [
  { id: "nav-overview", group: "Go to", label: "Mission Control", run: () => {} },
  { id: "act-refresh", group: "Actions", label: "Refresh data", hint: "R", run: () => {} },
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
});
