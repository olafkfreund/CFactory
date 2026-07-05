import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import PortalSwitcher from "./PortalSwitcher";
import { PORTALS } from "./dashboard";

// Portal switcher (#149). The four portals render as one control; exactly one is
// the current (non-link) entry and the rest are out-links.
describe("PortalSwitcher", () => {
  it("has all four portals with exactly one current", () => {
    expect(PORTALS).toHaveLength(4);
    expect(PORTALS.filter((p) => p.current)).toHaveLength(1);
    expect(PORTALS.find((p) => p.current)?.svc).toBe("CFactory");
  });

  it("renders the current portal as a non-link and the others as links", () => {
    const html = renderToStaticMarkup(<PortalSwitcher />);
    expect(html).toContain('aria-current="page"');
    expect(html).toContain("Cockpit");
    // sibling portals link out to their hosts
    expect(html).toContain("pfactory");
    expect(html).toContain('aria-label="Factory portals"');
  });
});
