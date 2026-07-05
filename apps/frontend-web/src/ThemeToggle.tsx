// Theme toggle (#150) — a compact Dark / Light / System segmented control for the
// topbar. Self-contained: it reads the stored preference, applies + persists on
// change, and (for "system") re-applies when the OS scheme changes while selected.
import { useEffect, useState } from "react";
import { applyTheme, setTheme, storedTheme, THEMES, type Theme } from "./theme";

const LABEL: Record<Theme, string> = { dark: "Dark", light: "Light", system: "System" };

export default function ThemeToggle() {
  const [theme, setThemeState] = useState<Theme>(() => storedTheme());

  // Follow OS changes while on "system".
  useEffect(() => {
    if (theme !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => {
      applyTheme("system");
    };
    mq.addEventListener("change", onChange);
    return () => {
      mq.removeEventListener("change", onChange);
    };
  }, [theme]);

  const pick = (t: Theme) => {
    setThemeState(t);
    setTheme(t);
  };

  return (
    <div className="theme-toggle" role="group" aria-label="Colour theme">
      {THEMES.map((t) => (
        <button
          key={t}
          className={`theme-opt ${theme === t ? "on" : ""}`}
          aria-pressed={theme === t}
          onClick={() => {
            pick(t);
          }}
        >
          {LABEL[t]}
        </button>
      ))}
    </div>
  );
}
