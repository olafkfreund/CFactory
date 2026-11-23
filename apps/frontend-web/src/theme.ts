// Shared tokens use .dark; the system preference follows the OS.
export type Theme = "light" | "dark" | "system";

const KEY = "cfactory-theme";
export const THEMES: Theme[] = ["dark", "light", "system"];

/** Stored preference, defaulting to the OS. */
export function storedTheme(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* localStorage unavailable — fall through to default */
  }
  return "system";
}

/** Resolve a preference to the concrete mode to render. */
export function resolveTheme(t: Theme): "light" | "dark" {
  if (t !== "system") return t;
  return typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: light)").matches
    ? "light"
    : "dark";
}

/** Toggle the shared dark token ramp. */
export function applyTheme(t: Theme): void {
  const mode = resolveTheme(t);
  const el = document.documentElement;
  el.classList.toggle("dark", mode === "dark");
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
