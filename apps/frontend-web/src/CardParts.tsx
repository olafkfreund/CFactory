// The card chrome the Backlog list and the planning Kanban board share
// (RFC-0019 Phase 1, #302): the filter bar and the card body with its move /
// reprioritise controls. Split from cards.ts so this file exports components
// only (react-refresh) and the two views hold nothing but their own layout.
import type { ReactNode } from "react";
import type { Card, CardFilters, CardPatch, CardStatus } from "./api";
import {
  CARD_STAGE_ACTIONS,
  CARD_STAGES,
  CARD_STATUSES,
  CARD_TIERS,
  peek,
  stageBlocker,
  type StageAction,
} from "./cards";
import Markdown from "./Markdown";

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
 * The error + notice banners both views show above their cards.
 *
 * One component rather than a copy per view: a refusal and a "that stage was
 * skipped" notice are different things and must not read the same, so keeping the
 * pair together is what stops one view rendering only half of them.
 */
export function CardBanners({ error, notice }: { error?: string | null; notice?: string | null }) {
  return (
    <>
      {error && <div className="banner banner--error">{error}</div>}
      {notice && <div className="banner banner--info">{notice}</div>}
    </>
  );
}

/**
 * The plan / code / test / run-all buttons (RFC-0020 §3.7).
 *
 * Rendered inside `CardBody`, so the Backlog list and the Kanban board get them
 * from the same place — the alternative was the same four buttons written twice
 * and drifting.
 *
 * A button is disabled when the backend WOULD refuse it, with the reason as its
 * title (see `stageBlocker`); each stage also shows what its dispatch record
 * currently says, so "already running" and "already done" are visible rather than
 * merely implied by a greyed-out button.
 */
export function CardStageActions({
  card,
  busy,
  onStage,
}: {
  card: Card;
  busy: boolean;
  onStage: (action: StageAction) => void;
}) {
  return (
    <div className="card-pl__stages" role="group" aria-label={`Stage actions for ${card.card_key}`}>
      {CARD_STAGE_ACTIONS.map(({ key, label, hint }) => {
        const blocked = stageBlocker(card, key);
        const state = key === "run" ? undefined : card.stage_runs[key]?.status;
        return (
          <button
            key={key}
            className={`card-btn card-btn--stage${state ? ` card-btn--stage-${state}` : ""}`}
            disabled={busy || blocked !== null}
            title={blocked ?? hint}
            aria-label={`${label} ${card.card_key}`}
            onClick={() => {
              onStage(key);
            }}
          >
            {label}
            {state && <span className="card-stage-dot" aria-hidden="true" />}
          </button>
        );
      })}
    </div>
  );
}

/**
 * The imported issue body plus acceptance criteria, collapsed (#213).
 *
 * `<details>` rather than a `useState` toggle: the collapse is the platform's, so
 * it is keyboard-operable and screen-reader-announced without any code, and
 * `CardBody` stays a pure function of its props. Markdown goes through the shared
 * `Markdown` component, which renders React nodes and never HTML — issue bodies
 * are third-party text and must not be able to inject markup.
 */
function CardDetails({ card }: { card: Card }) {
  const body = card.description?.trim() ?? "";
  const acs = card.acceptance_criteria;
  if (!body && acs.length === 0) return null;
  return (
    <details className="card-pl__body">
      <summary className="card-pl__peek">
        <span className="card-pl__peek-text">
          {body ? peek(body) : `${String(acs.length)} acceptance criteria`}
        </span>
      </summary>
      <div className="card-pl__detail">
        {acs.length > 0 && (
          <>
            <div className="card-pl__detail-h">Acceptance criteria</div>
            <ul className="card-pl__ac">
              {acs.map((ac, i) => (
                <li key={i}>{ac}</li>
              ))}
            </ul>
          </>
        )}
        {body && <Markdown text={body} className="card-md" />}
      </div>
    </details>
  );
}

/**
 * The card body both views render: key, title, the collapsed issue body and
 * acceptance criteria, tier / assignee / milestone / label / issue chips, the two
 * mutation affordances — a status `<select>` (move) and ▲/▼ priority nudges
 * (reprioritise) — and, when `onStage` is supplied, the plan / code / test /
 * run-all stage actions.
 *
 * `issueHref` is passed in rather than derived here: the host lives on the
 * connection this card's repository is reached through, and the whole
 * repository-to-host map is one fetch for the list rather than one per card (see
 * `issueUrlResolver`). Null means the host is unknown or the provider's URL shape
 * is not derivable, and the ref renders as text instead.
 *
 * ponytail: keyboard-first controls rather than drag-and-drop. They are
 * accessible for free and hit both PATCH paths; DnD can be layered on top of
 * the same `onMutate` callback later if it's actually asked for.
 */
export function CardBody({
  card,
  busy,
  issueHref,
  onMutate,
  onStage,
  onEdit,
  onDelete,
}: {
  card: Card;
  busy: boolean;
  issueHref?: string | null;
  onMutate: (patch: CardPatch) => void;
  onStage?: (action: StageAction) => void;
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
      <CardDetails card={card} />
      <div className="card-pl__chips">
        {card.tier && <span className={`card-tier card-tier--${card.tier}`}>{card.tier}</span>}
        {card.issue_state && (
          <span
            className={`card-chip card-chip--state-${card.issue_state}`}
            title={`Issue state on the git host: ${card.issue_state}`}
          >
            {card.issue_state}
          </span>
        )}
        {card.issue_ref &&
          (issueHref ? (
            <a
              className="card-chip card-chip--issue"
              href={issueHref}
              target="_blank"
              rel="noreferrer noopener"
              title={`Open ${card.issue_ref} on the git host`}
            >
              {card.issue_ref} ↗
            </a>
          ) : (
            <span
              className="card-chip"
              title="No link — this board's git host is not configured, or its issue URLs are not derivable"
            >
              {card.issue_ref}
            </span>
          ))}
        {card.assignee && <span className="card-chip">@{card.assignee}</span>}
        {card.milestone && <span className="card-chip">◇ {card.milestone}</span>}
        {card.labels.map((label) => (
          <span className="card-chip card-chip--label" key={label}>
            {label}
          </span>
        ))}
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
        {CARD_STAGES.filter((s) => card.stage_runs[s]).map((s) => {
          const run = card.stage_runs[s];
          if (!run) return null;
          return (
            <span
              key={s}
              className={`card-chip card-stage-chip card-stage-chip--${run.status}`}
              title={run.detail ? `${s}: ${run.status} (${run.detail})` : `${s}: ${run.status}`}
            >
              {s} · {run.status}
            </span>
          );
        })}
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
      {onStage && <CardStageActions card={card} busy={busy} onStage={onStage} />}
    </article>
  );
}
