import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import {
  ProjectBreadcrumb,
  filterBreadcrumbProjects,
  stepBreadcrumbKey,
  type BreadcrumbProject,
} from "./ProjectBreadcrumb";

const projects: BreadcrumbProject[] = [
  { slug: "alpha", name: "Alpha App" },
  { slug: "beta", name: "Beta" },
];

describe("ProjectBreadcrumb", () => {
  it("shows the current project when currentSlug is set and All projects when unset", () => {
    const activeHtml = renderToStaticMarkup(
      <ProjectBreadcrumb projects={projects} currentSlug="beta" onSelect={vi.fn()} />,
    );
    expect(activeHtml).toContain('aria-haspopup="listbox"');
    expect(activeHtml).toContain('aria-expanded="false"');
    expect(activeHtml).toContain("Project");
    expect(activeHtml).toContain("Beta");

    const allHtml = renderToStaticMarkup(
      <ProjectBreadcrumb projects={projects} onSelect={vi.fn()} />,
    );
    expect(allHtml).toContain("All projects");
  });

  it("renders listbox options, current marker, All projects..., and no delete or add controls when open", () => {
    const html = renderToStaticMarkup(
      <ProjectBreadcrumb
        projects={projects}
        currentSlug="beta"
        onSelect={vi.fn()}
        onViewAll={vi.fn()}
        initialOpen
      />,
    );
    expect(html).toContain('aria-expanded="true"');
    expect(html).toContain('role="combobox"');
    expect(html).toContain('aria-label="Search projects"');
    expect(html).toContain('role="listbox"');
    expect(html).toContain('role="option"');
    expect(html).toContain('aria-current="true"');
    expect(html).toContain("All projects...");
    expect(html).not.toMatch(/delete|remove|add project/i);
  });

  it("filters projects by search query and renders empty state when none match", () => {
    expect(filterBreadcrumbProjects(projects, "alp")).toEqual([
      { slug: "alpha", name: "Alpha App" },
    ]);
    expect(filterBreadcrumbProjects(projects, "BETA")).toEqual([
      { slug: "beta", name: "Beta" },
    ]);
    expect(filterBreadcrumbProjects(projects, "zzz")).toEqual([]);

    const filteredHtml = renderToStaticMarkup(
      <ProjectBreadcrumb
        projects={projects}
        currentSlug="beta"
        onSelect={vi.fn()}
        initialOpen
        initialQuery="alp"
      />,
    );
    expect(filteredHtml).toContain("Alpha App");
    expect(filteredHtml.match(/role="option"/g)).toHaveLength(1);
    expect(filteredHtml).not.toContain("-opt-beta");

    const emptyHtml = renderToStaticMarkup(
      <ProjectBreadcrumb
        projects={projects}
        currentSlug="beta"
        onSelect={vi.fn()}
        initialOpen
        initialQuery="zzz"
      />,
    );
    expect(emptyHtml).toContain("No matching projects");
  });

  it("exposes the highlighted option through aria-activedescendant and steps keyboard navigation", () => {
    const html = renderToStaticMarkup(
      <ProjectBreadcrumb
        projects={projects}
        currentSlug="alpha"
        onSelect={vi.fn()}
        initialOpen
        initialSelectedIndex={1}
      />,
    );
    expect(html).toMatch(/aria-activedescendant="[^"]+-opt-beta"/);
    expect(html).toMatch(/id="[^"]+-opt-beta"[^>]*aria-selected="true"/);

    expect(stepBreadcrumbKey("ArrowDown", projects, 0)).toEqual({ action: "move", sel: 1 });
    expect(stepBreadcrumbKey("ArrowDown", projects, 1)).toEqual({ action: "move", sel: 0 });
    expect(stepBreadcrumbKey("ArrowUp", projects, 0)).toEqual({ action: "move", sel: 1 });
    expect(stepBreadcrumbKey("Enter", projects, 1)).toEqual({ action: "select", slug: "beta" });
    expect(stepBreadcrumbKey("Escape", projects, 1)).toEqual({ action: "close" });
  });
});
