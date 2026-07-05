import { afterEach, describe, expect, it, vi } from "vitest";

import { resolveTheme, storedTheme } from "./theme";

// Theme logic (#150). Pure resolution + storage parsing; matchMedia and
// localStorage are stubbed on the (jsdom-less) global so the branches are
// exercised without a DOM environment.
function stubMatchMedia(prefersLight: boolean) {
  vi.stubGlobal("window", {
    matchMedia: (q: string) => ({ matches: prefersLight && q.includes("light") }),
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("theme", () => {
  it("resolves explicit choices without consulting the OS", () => {
    expect(resolveTheme("light")).toBe("light");
    expect(resolveTheme("dark")).toBe("dark");
  });

  it("resolves 'system' from the OS preference", () => {
    stubMatchMedia(true);
    expect(resolveTheme("system")).toBe("light");
    stubMatchMedia(false);
    expect(resolveTheme("system")).toBe("dark");
  });

  it("defaults to dark when nothing is stored or storage is unavailable", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => null,
      setItem: () => {},
    });
    expect(storedTheme()).toBe("dark");
  });

  it("returns a valid stored preference", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => "light",
      setItem: () => {},
    });
    expect(storedTheme()).toBe("light");
  });
});
