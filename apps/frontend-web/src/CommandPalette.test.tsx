import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import CommandPalette, { type PaletteCommand } from "./CommandPalette";
import type { WorkItem } from "./api";

// Command palette (#147). Rendered to static markup (react-dom/server, no jsdom —
// matches the other frontend tests). We assert the closed/open contract, that
// commands render grouped, and that tasks only surface once there's a query — the
// bare palette must not dump every work item.
const cmds: PaletteCommand[] = [
  { id: "nav-overview", group: "Go to", label: "Mission Control", run: () => {} },
  { id: "act-refresh", group: "Actions", label: "Refresh data", hint: "R", run: () => {} },
];

const items = [
  { correlation_key: "006", title: "Orders Total API" },
  { correlation_key: "004", title: "Orders webhook consumer" },
] as unknown as WorkItem[];

describe("CommandPalette", () => {
  it("renders nothing when closed", () => {
    const html = renderToStaticMarkup(
      <CommandPalette
        open={false}
        onClose={() => {}}
        commands={cmds}
        items={items}
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
        items={items}
        onOpenTask={() => {}}
      />,
    );
    expect(html).toContain("Command palette");
    expect(html).toContain("Mission Control");
    expect(html).toContain("Go to");
    expect(html).toContain("Actions");
    // tasks are query-gated → the work-item titles must not appear on a bare open
    expect(html).not.toContain("Orders Total API");
    // the first row is selected by default
    expect(html).toContain('aria-selected="true"');
  });
});
