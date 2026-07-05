// Theme control (#150). The cockpit ships dark + a Gruvbox light mode; the actual
// palette flip is a token-override layer in index.css (:root.light), so this file
// only decides light-vs-dark and toggles the `light` class on <html>. "system"
// follows the OS. Kept tiny and pure-ish (only matchMedia/localStorage as I/O) so
// the resolve logic is unit-testable. A pre-paint guard in index.html applies the
// same decision before React mounts to avoid a flash.
export type Theme = "light" | "dark" | "system";

const KEY = "cfactory-theme";
export const THEMES: Theme[] = ["dark", "light", "system"];

/** Stored preference, defaulting to dark (the cockpit's long-standing look). */
export function storedTheme(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* localStorage unavailable — fall through to default */
  }
  return "dark";
}

/** Resolve a preference to the concrete mode to render. */
export function resolveTheme(t: Theme): "light" | "dark" {
  if (t !== "system") return t;
  return typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: light)").matches
    ? "light"
    : "dark";
}

/** Toggle the `light` class + keep the html data-theme attribute honest. */
export function applyTheme(t: Theme): void {
  const mode = resolveTheme(t);
  const el = document.documentElement;
  el.classList.toggle("light", mode === "light");
  el.setAttribute("data-theme", mode);
}

/** Persist a preference and apply it immediately. */
export function setTheme(t: Theme): void {
  try {
    localStorage.setItem(KEY, t);
  } catch {
    /* ignore persistence failure; still apply for this session */
  }
  applyTheme(t);
}
