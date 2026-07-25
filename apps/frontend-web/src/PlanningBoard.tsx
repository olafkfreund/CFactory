// Planning board — the same cards as the Backlog, laid out as a Kanban whose
// columns are the card STATUSES (RFC-0019 Phase 1, #302). Moving a card between
// columns PATCHes `status`; the ▲/▼ nudges PATCH `priority`. Both go through the
// shared optimistic-with-rollback path in cards.ts.
//
// This is the PLANNING axis. Board.tsx is the PARR pipeline (plan → code → test
// execution stages) and is a different view over different data.
import { useState, type CSSProperties } from "react";
import { importCards, type CardFilters } from "./api";
import { byPriority, CARD_STATUSES, matchesQuery, useCards } from "./cards";
import { CardBody, CardFilterBar } from "./CardParts";

export default function PlanningBoard({ reloadSignal }: { reloadSignal: number }) {
  // Status is the column here, so it is never a server-side filter on this view.
  const [filters, setFilters] = useState<CardFilters>({});
  const [query, setQuery] = useState("");
  const { cards, loading, error, mutate, reload, setError } = useCards(filters, reloadSignal);
  const [importing, setImporting] = useState(false);
  const [imported, setImported] = useState<string | null>(null);

  const shown = cards.filter((c) => matchesQuery(c, query)).sort(byPriority);

  // Pull the connected repository's EXISTING issues onto the board (RFC-0020
  // §3.6). Poll-based, not live — the summary says when it last synced, and says
  // so out loud when CFACTORY_IMPORT_MAX truncated the run.
  async function runImport() {
    setImporting(true);
    try {
      const r = await importCards();
      if (!r.ok) throw new Error(r.reason ?? "import failed");
      const truncated = r.truncated ? " (truncated — raise CFACTORY_IMPORT_MAX)" : "";
      setImported(
        `Imported ${String(r.imported)}, updated ${String(r.updated)}, ` +
          `skipped ${String(r.skipped)} from ${r.project}` +
          `${truncated}. Last synced ${r.last_synced_at ?? "never"} — polled, not live.`,
      );
      setError(null);
      reload();
    } catch (e) {
      setImported(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setImporting(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Planning board</h1>
        <p>
          Cards by planning status — move a card with its status control, reorder it with ▲/▼. Every
          change is written straight through to the card API.
        </p>
        <button
          className="card-btn"
          type="button"
          onClick={() => {
            void runImport();
          }}
          disabled={importing}
        >
          {importing ? "Importing…" : "Import repo issues"}
        </button>
      </div>

      {imported && <div className="banner">{imported}</div>}

      <CardFilterBar
        query={query}
        onQuery={setQuery}
        filters={filters}
        onFilters={setFilters}
        hideStatus
      />

      {error && <div className="banner banner--error">{error}</div>}

      <section
        className="board"
        aria-label="Planning board"
        style={{ "--board-cols": CARD_STATUSES.length } as CSSProperties}
      >
        {CARD_STATUSES.map((col) => {
          const column = shown.filter((c) => c.status === col.key);
          return (
            <div className={`column column--card-${col.cls}`} key={col.key}>
              <div className="column-head">
                <span className="column-dot" />
                <span className="column-title">{col.label}</span>
                <span className="column-count">{column.length}</span>
              </div>
              <div className="column-body">
                {column.length === 0 ? (
                  <p className="col-empty">{loading ? "Loading…" : "Empty"}</p>
                ) : (
                  column.map((card) => (
                    <CardBody
                      key={card.card_key}
                      card={card}
                      busy={false}
                      onMutate={(patch) => {
                        void mutate(card.card_key, patch);
                      }}
                    />
                  ))
                )}
              </div>
            </div>
          );
        })}
      </section>
    </>
  );
}
