// Command palette (⌘K) — jump to any view or task and run actions from the
// keyboard (#147). Self-contained: it takes the flat command list its host
// assembles (navigation + actions), filters those with a case-insensitive
// substring match, and drives selection with the keyboard. No router — the
// cockpit is state-routed — so "go to" calls the host's setView and "open task"
// hands the host a correlation key to render in TaskDetail.
//
// Task results are FEDERATED (#149): as you type, the palette queries the
// cockpit's cross-portal /api/search (debounced), so a task number, title,
// repo, or status matches across all four portals — not just the work items
// already loaded on screen.
//
// House style: hand-rolled overlay (matches the Copilot popup / no extra deps),
// role="dialog" + a listbox of role="option" rows, Escape to close, ↑↓ to move,
// Enter to run. Animation is a CSS fade gated by prefers-reduced-motion.
import { useEffect, useMemo, useRef, useState } from "react";
import type { ComponentType } from "react";
import { searchWorkItems, type SearchResult } from "./api";

export type PaletteCommand = {
  id: string;
  group: "Go to" | "Actions";
  label: string;
  hint?: string; // right-aligned key hint, e.g. "G P"
  keywords?: string;
  Icon?: ComponentType<{ size?: number }>;
  run: () => void;
};

type Row =
  | { kind: "cmd"; group: string; cmd: PaletteCommand }
  | { kind: "task"; group: string; result: SearchResult };

function matches(hay: string, q: string): boolean {
  return hay.toLowerCase().includes(q);
}

export default function CommandPalette({
  open,
  onClose,
  commands,
  onOpenTask,
}: {
  open: boolean;
  onClose: () => void;
  commands: PaletteCommand[];
  onOpenTask: (correlationKey: string) => void;
}) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [results, setResults] = useState<SearchResult[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Reset query + selection each time the palette opens, and focus the input.
  useEffect(() => {
    if (open) {
      setQ("");
      setSel(0);
      // focus after paint so the field is ready for the first keystroke
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  // Federated task search (#149): debounce the query and hit /api/search. A
  // blank query clears results; a stale in-flight response is discarded so the
  // list always reflects the latest keystroke.
  useEffect(() => {
    const needle = q.trim();
    if (!open || !needle) {
      setResults([]);
      return;
    }
    let stale = false;
    const timer = setTimeout(() => {
      searchWorkItems(needle, 8)
        .then((r) => {
          if (!stale) setResults(r);
        })
        .catch(() => {
          if (!stale) setResults([]);
        });
    }, 140);
    return () => {
      stale = true;
      clearTimeout(timer);
    };
  }, [q, open]);

  const rows: Row[] = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const cmdRows: Row[] = commands
      .filter((c) => !needle || matches(`${c.label} ${c.keywords ?? ""}`, needle))
      .map((c) => ({ kind: "cmd", group: c.group, cmd: c }));
    // Tasks only surface once there's a query — a bare palette shouldn't dump
    // every work item; it should read as "jump/act", then become search.
    const taskRows: Row[] = !needle
      ? []
      : results.map((result) => ({ kind: "task", group: "Tasks", result }));
    // Order groups: Tasks first when searching (most specific), then Go to, Actions.
    return [...taskRows, ...cmdRows];
  }, [q, commands, results]);

  // Keep the selection in range as the result set shrinks/grows.
  useEffect(() => {
    setSel((s) => Math.max(0, Math.min(s, rows.length - 1)));
  }, [rows.length]);

  if (!open) return null;

  const runRow = (row: Row | undefined) => {
    if (!row) return;
    if (row.kind === "cmd") row.cmd.run();
    else onOpenTask(row.result.correlation_key);
    onClose();
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel((s) => (rows.length ? (s + 1) % rows.length : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((s) => (rows.length ? (s - 1 + rows.length) % rows.length : 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      runRow(rows[sel]);
    }
  };

  // Group headers are rendered inline as the group changes while iterating rows.
  let lastGroup = "";

  return (
    <div className="cmdk-scrim" onMouseDown={onClose}>
      <div
        className="cmdk"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onMouseDown={(e) => {
          e.stopPropagation();
        }}
        onKeyDown={onKey}
      >
        <div className="cmdk-in">
          <span className="cmdk-q" aria-hidden="true" />
          <input
            ref={inputRef}
            className="cmdk-input"
            placeholder="Search tasks across portals, or jump to a view…"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
            }}
            role="combobox"
            aria-expanded="true"
            aria-controls="cmdk-list"
            aria-activedescendant={rows[sel] ? `cmdk-row-${String(sel)}` : undefined}
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="cmdk-kbd">esc</kbd>
        </div>

        <div className="cmdk-list" id="cmdk-list" role="listbox" ref={listRef}>
          {rows.length === 0 && (
            <div className="cmdk-empty">No matches — try a task number, a view, or an action.</div>
          )}
          {rows.map((row, i) => {
            const header = row.group !== lastGroup ? ((lastGroup = row.group), row.group) : null;
            const selected = i === sel;
            return (
              <div key={row.kind === "cmd" ? row.cmd.id : `t-${row.result.correlation_key}`}>
                {header && <div className="cmdk-gl">{header}</div>}
                <div
                  id={`cmdk-row-${String(i)}`}
                  role="option"
                  aria-selected={selected}
                  className={`cmdk-row ${selected ? "sel" : ""}`}
                  onMouseMove={() => {
                    setSel(i);
                  }}
                  onClick={() => {
                    runRow(row);
                  }}
                >
                  <span className="cmdk-ic" aria-hidden="true">
                    {row.kind === "cmd" && row.cmd.Icon ? <row.cmd.Icon size={15} /> : null}
                  </span>
                  <span className="cmdk-label">
                    {row.kind === "cmd" ? (
                      row.cmd.label
                    ) : (
                      <>
                        <span className="cmdk-key">#{row.result.correlation_key}</span>
                        {row.result.title ? ` · ${row.result.title}` : ""}
                      </>
                    )}
                  </span>
                  {row.kind === "cmd" && row.cmd.hint && (
                    <span className="cmdk-hint">{row.cmd.hint}</span>
                  )}
                  {row.kind === "task" && row.result.status && (
                    <span className="cmdk-hint">{row.result.status}</span>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        <div className="cmdk-foot">
          <span>
            <b>↑↓</b> navigate
          </span>
          <span>
            <b>↵</b> open
          </span>
          <span>
            <b>esc</b> close
          </span>
        </div>
      </div>
    </div>
  );
}
