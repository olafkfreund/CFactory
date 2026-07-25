// The card chrome the Backlog list and the planning Kanban board share
// (RFC-0019 Phase 1, #302): the filter bar and the card body with its move /
// reprioritise controls. Split from cards.ts so this file exports components
// only (react-refresh) and the two views hold nothing but their own layout.
import type { ReactNode } from "react";
import type { Card, CardFilters, CardPatch, CardStatus } from "./api";
import { CARD_STATUSES, CARD_TIERS } from "./cards";

/**
 * The filter bar both views share: a free-text query plus the server-side
 * status/tier/assignee/milestone filters. `hideStatus` drops the status filter
 * on the Kanban board, where status IS the column.
 */
export function CardFilterBar({
  query,
  onQuery,
  filters,
  onFilters,
  hideStatus,
  children,
}: {
  query: string;
  onQuery: (q: string) => void;
  filters: CardFilters;
  onFilters: (f: CardFilters) => void;
  hideStatus?: boolean;
  children?: ReactNode;
}) {
  const set = (k: keyof CardFilters, v: string) => {
    onFilters({ ...filters, [k]: v || undefined });
  };
  return (
    <div className="rt-toolbar board-toolbar">
      <input
        className="board-search"
        type="search"
        placeholder="Filter by key, title, assignee…"
        value={query}
        onChange={(e) => {
          onQuery(e.target.value);
        }}
        aria-label="Filter cards"
      />
      {!hideStatus && (
        <select
          className="card-select"
          value={filters.status ?? ""}
          onChange={(e) => {
            set("status", e.target.value);
          }}
          aria-label="Filter by status"
        >
          <option value="">status: any</option>
          {CARD_STATUSES.map((s) => (
            <option key={s.key} value={s.key}>
              {s.label}
            </option>
          ))}
        </select>
      )}
      <select
        className="card-select"
        value={filters.tier ?? ""}
        onChange={(e) => {
          set("tier", e.target.value);
        }}
        aria-label="Filter by tier"
      >
        <option value="">tier: any</option>
        {CARD_TIERS.map((t) => (
          <option key={t} value={t}>
            {t}
          </option>
        ))}
      </select>
      <input
        className="card-input card-input--sm"
        placeholder="assignee"
        value={filters.assignee ?? ""}
        onChange={(e) => {
          set("assignee", e.target.value);
        }}
        aria-label="Filter by assignee"
      />
      <input
        className="card-input card-input--sm"
        placeholder="milestone"
        value={filters.milestone ?? ""}
        onChange={(e) => {
          set("milestone", e.target.value);
        }}
        aria-label="Filter by milestone"
      />
      <span className="board-toolbar-spacer" />
      {children}
    </div>
  );
}

/**
 * The card body both views render: key, title, acceptance criteria count, tier
 * / assignee / milestone / correlation chips, plus the two mutation affordances
 * — a status `<select>` (move) and ▲/▼ priority nudges (reprioritise).
 *
 * ponytail: keyboard-first controls rather than drag-and-drop. They are
 * accessible for free and hit both PATCH paths; DnD can be layered on top of
 * the same `onMutate` callback later if it's actually asked for.
 */
export function CardBody({
  card,
  busy,
  onMutate,
  onEdit,
  onDelete,
}: {
  card: Card;
  busy: boolean;
  onMutate: (patch: CardPatch) => void;
  onEdit?: () => void;
  onDelete?: () => void;
}) {
  return (
    <article className={`card-pl ${busy ? "card-pl--busy" : ""}`}>
      <div className="card-pl__head">
        <span className="wi-key" title={card.card_key}>
          {card.card_key}
        </span>
        <span className="card-pl__prio mono" title="Priority — lower sorts higher">
          P{card.priority}
        </span>
      </div>
      <div className="card-pl__title">{card.title}</div>
      <div className="card-pl__chips">
        {card.tier && <span className={`card-tier card-tier--${card.tier}`}>{card.tier}</span>}
        {card.assignee && <span className="card-chip">@{card.assignee}</span>}
        {card.milestone && <span className="card-chip">◇ {card.milestone}</span>}
        {card.acceptance_criteria.length > 0 && (
          <span className="card-chip" title={card.acceptance_criteria.join("\n")}>
            {card.acceptance_criteria.length} AC
          </span>
        )}
        {card.correlation_key && (
          <span className="card-chip card-chip--live" title="In the factory">
            ⟳ {card.correlation_key}
          </span>
        )}
      </div>
      <div className="card-pl__actions">
        <select
          className="card-select card-select--move"
          value={card.status}
          disabled={busy}
          onChange={(e) => {
            onMutate({ status: e.target.value as CardStatus });
          }}
          aria-label={`Status of ${card.card_key}`}
        >
          {CARD_STATUSES.map((s) => (
            <option key={s.key} value={s.key}>
              {s.label}
            </option>
          ))}
        </select>
        <button
          className="card-btn"
          disabled={busy}
          onClick={() => {
            onMutate({ priority: Math.max(0, card.priority - 1) });
          }}
          title="Raise priority"
          aria-label={`Raise priority of ${card.card_key}`}
        >
          ▲
        </button>
        <button
          className="card-btn"
          disabled={busy}
          onClick={() => {
            onMutate({ priority: card.priority + 1 });
          }}
          title="Lower priority"
          aria-label={`Lower priority of ${card.card_key}`}
        >
          ▼
        </button>
        {onEdit && (
          <button className="card-btn" disabled={busy} onClick={onEdit}>
            Edit
          </button>
        )}
        {onDelete && (
          <button
            className="card-btn card-btn--danger"
            disabled={busy}
            onClick={onDelete}
            aria-label={`Delete ${card.card_key}`}
          >
            Delete
          </button>
        )}
      </div>
    </article>
  );
}
