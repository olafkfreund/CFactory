/* eslint-disable react-refresh/only-export-components */
// Project breadcrumb (#570) — topbar project scope picker next to the portal
// switcher. Mirrors the shared/factory-ui/ProjectBreadcrumb.tsx prop contract
// and keyboard/a11y semantics using CFactory's Gruvbox CSS classes (.proj-bc*).
import { useEffect, useId, useMemo, useRef, useState } from "react";

export interface BreadcrumbProject {
  slug: string;
  name: string;
}

export function filterBreadcrumbProjects(
  projects: BreadcrumbProject[],
  query: string,
): BreadcrumbProject[] {
  const needle = query.trim().toLowerCase();
  return projects.filter(
    (p) => !needle || `${p.name} ${p.slug}`.toLowerCase().includes(needle),
  );
}

export type BreadcrumbKeyAction =
  | { action: "close" }
  | { action: "move"; sel: number }
  | { action: "select"; slug: string | undefined }
  | { action: "none" };

export function stepBreadcrumbKey(
  key: string,
  matches: BreadcrumbProject[],
  sel: number,
): BreadcrumbKeyAction {
  if (key === "Escape") {
    return { action: "close" };
  }
  if (key === "ArrowDown") {
    return { action: "move", sel: matches.length ? (sel + 1) % matches.length : 0 };
  }
  if (key === "ArrowUp") {
    return {
      action: "move",
      sel: matches.length ? (sel - 1 + matches.length) % matches.length : 0,
    };
  }
  if (key === "Enter") {
    return { action: "select", slug: matches[sel]?.slug };
  }
  return { action: "none" };
}

export function ProjectBreadcrumb({
  projects,
  currentSlug,
  onSelect,
  onViewAll,
  initialOpen = false,
  initialQuery = "",
  initialSelectedIndex = 0,
}: {
  projects: BreadcrumbProject[];
  currentSlug?: string | undefined;
  onSelect: (slug: string) => void;
  onViewAll?: (() => void) | undefined;
  initialOpen?: boolean | undefined;
  initialQuery?: string | undefined;
  initialSelectedIndex?: number | undefined;
}) {
  const [open, setOpen] = useState(initialOpen);
  const [q, setQ] = useState(initialQuery);
  const [sel, setSel] = useState(initialSelectedIndex);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const uid = useId();
  const listId = `${uid}-list`;
  const optId = (slug: string): string => `${uid}-opt-${slug}`;
  const current = projects.find((p) => p.slug === currentSlug);

  const matches = useMemo(() => filterBreadcrumbProjects(projects, q), [projects, q]);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (root.current && e.target instanceof Node && !root.current.contains(e.target)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", away);
    return () => {
      document.removeEventListener("mousedown", away);
    };
  }, [open]);

  useEffect(() => {
    setSel((s) => Math.max(0, Math.min(s, matches.length - 1)));
  }, [matches.length]);

  // Keep the highlighted row visible when arrow keys move past the list viewport.
  useEffect(() => {
    const active = matches[sel];
    if (open && active) {
      document.getElementById(`${uid}-opt-${active.slug}`)?.scrollIntoView({ block: "nearest" });
    }
  }, [open, sel, matches, uid]);

  const toggle = () => {
    setOpen((o) => !o);
    setQ("");
    setSel(0);
  };

  const pick = (slug: string | undefined) => {
    if (!slug) return;
    setOpen(false);
    onSelect(slug);
  };

  const onKey = (e: React.KeyboardEvent) => {
    const next = stepBreadcrumbKey(e.key, matches, sel);
    if (next.action === "close") {
      e.preventDefault();
      setOpen(false);
      trigger.current?.focus();
    } else if (next.action === "move") {
      e.preventDefault();
      setSel(next.sel);
    } else if (next.action === "select") {
      e.preventDefault();
      pick(next.slug);
    }
  };

  const activeMatch = matches[sel];

  return (
    <div ref={root} className="proj-bc">
      <button
        ref={trigger}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={toggle}
        className="proj-bc-btn"
      >
        <span className="proj-bc-prefix">Project</span>
        <span className="proj-bc-sep">/</span>
        <span className="proj-bc-name">{current?.name ?? "All projects"}</span>
        <span aria-hidden="true" className="proj-bc-caret">
          ▾
        </span>
      </button>
      {open && (
        <div className="proj-bc-menu">
          <input
            autoFocus
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setSel(0);
            }}
            onKeyDown={onKey}
            placeholder="Search projects..."
            aria-label="Search projects"
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-activedescendant={activeMatch ? optId(activeMatch.slug) : undefined}
            autoComplete="off"
            spellCheck={false}
            className="proj-bc-input"
          />
          <ul id={listId} role="listbox" aria-label="Projects" className="proj-bc-list">
            {matches.length === 0 && (
              <li className="proj-bc-empty">No matching projects</li>
            )}
            {matches.map((p, i) => (
              <li
                key={p.slug}
                id={optId(p.slug)}
                role="option"
                aria-selected={i === sel}
                aria-current={p.slug === currentSlug ? "true" : undefined}
                onMouseMove={() => {
                  setSel(i);
                }}
                onClick={() => {
                  pick(p.slug);
                }}
                className={`proj-bc-opt ${i === sel ? "sel" : ""}`}
              >
                <span className="proj-bc-opt-name">{p.name}</span>
                {p.slug === currentSlug && (
                  <span aria-hidden="true" className="proj-bc-check">
                    ✓
                  </span>
                )}
              </li>
            ))}
          </ul>
          {onViewAll && (
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                onViewAll();
              }}
              className="proj-bc-all"
            >
              All projects...
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default ProjectBreadcrumb;
