// Planning board — the same cards as the Backlog, laid out as a Kanban whose
// columns are the card STATUSES (RFC-0019 Phase 1, #302). Moving a card between
// columns PATCHes `status`; the ▲/▼ nudges PATCH `priority`. Both go through the
// shared optimistic-with-rollback path in cards.ts.
//
// This is the PLANNING axis. Board.tsx is the PARR pipeline (plan → code → test
// execution stages) and is a different view over different data.
import { useState, type CSSProperties } from "react";
import type { CardFilters } from "./api";
import { byPriority, CARD_STATUSES, matchesQuery, useCards, useIssueUrl } from "./cards";
import { CardBanners, CardBody, CardFilterBar } from "./CardParts";

export default function PlanningBoard({ reloadSignal }: { reloadSignal: number }) {
  // Status is the column here, so it is never a server-side filter on this view.
  const [filters, setFilters] = useState<CardFilters>({});
  const [query, setQuery] = useState("");
  const { cards, loading, error, notice, mutate, runStage, importing, importIssues } = useCards(
    filters,
    reloadSignal,
  );
  const hrefOf = useIssueUrl();

  const shown = cards.filter((c) => matchesQuery(c, query)).sort(byPriority);

  return (
    <>
      <div className="page-head">
        <h1>Planning board</h1>
        <p>
          Cards by planning status — move a card with its status control, reorder it with ▲/▼, and
          push it into plan, code or test (or all three) with its stage buttons. Every change is
          written straight through to the card API.
        </p>
        <button
          className="card-btn"
          type="button"
          onClick={() => {
            void importIssues();
          }}
          disabled={importing}
        >
          {importing ? "Importing…" : "Import repo issues"}
        </button>
      </div>

      <CardFilterBar
        query={query}
        onQuery={setQuery}
        filters={filters}
        onFilters={setFilters}
        hideStatus
      />

      <CardBanners error={error} notice={notice} />

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
                      issueHref={hrefOf(card.issue_ref)}
                      onMutate={(patch) => {
                        void mutate(card.card_key, patch);
                      }}
                      onStage={(action) => {
                        void runStage(card.card_key, action);
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
