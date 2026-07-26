// Backlog — the planning card list (RFC-0019 Phase 1, #302). One priority-ordered
// list of every card, filterable, with inline create and inline edit. The Kanban
// arrangement of the same cards lives in PlanningBoard.tsx; everything the two
// share is in cards.ts (data hook + optimistic mutation) and CardParts.tsx (chrome).
import { useState } from "react";
import {
  createCard,
  deleteCard,
  type Card,
  type CardFilters,
  type CardStatus,
  type CardTier,
} from "./api";
import {
  byPriority,
  CARD_STATUSES,
  CARD_TIERS,
  matchesQuery,
  useCards,
  useIssueUrl,
} from "./cards";
import { CardBanners, CardBody, CardFilterBar } from "./CardParts";

type Draft = {
  title: string;
  acceptance: string;
  status: CardStatus;
  priority: string;
  tier: string;
  assignee: string;
  milestone: string;
};

const EMPTY_DRAFT: Draft = {
  title: "",
  acceptance: "",
  status: "backlog",
  priority: "",
  tier: "",
  assignee: "",
  milestone: "",
};

const draftOf = (c: Card): Draft => ({
  title: c.title,
  acceptance: c.acceptance_criteria.join("\n"),
  status: c.status,
  priority: String(c.priority),
  tier: c.tier ?? "",
  assignee: c.assignee ?? "",
  milestone: c.milestone ?? "",
});

// One acceptance criterion per line; blank lines dropped.
const criteria = (s: string): string[] =>
  s
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

const nullable = (s: string): string | null => s.trim() || null;

export default function BacklogView({ reloadSignal }: { reloadSignal: number }) {
  const [filters, setFilters] = useState<CardFilters>({});
  const [query, setQuery] = useState("");
  const { cards, loading, error, notice, mutate, runStage, reload, setError } = useCards(
    filters,
    reloadSignal,
  );
  const hrefOf = useIssueUrl();

  // `create` = the new-card form; a card_key = that card's inline editor.
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [busy, setBusy] = useState(false);

  const shown = cards.filter((c) => matchesQuery(c, query)).sort(byPriority);

  function open(target: "create" | Card) {
    setEditing(target === "create" ? "create" : target.card_key);
    setDraft(target === "create" ? EMPTY_DRAFT : draftOf(target));
    setError(null);
  }

  async function save() {
    if (!draft.title.trim()) {
      setError("a card needs a title");
      return;
    }
    const fields = {
      title: draft.title.trim(),
      acceptance_criteria: criteria(draft.acceptance),
      status: draft.status,
      tier: (nullable(draft.tier) as CardTier | null) ?? null,
      assignee: nullable(draft.assignee),
      milestone: nullable(draft.milestone),
      ...(draft.priority.trim() ? { priority: Number(draft.priority) } : {}),
    };
    setBusy(true);
    try {
      if (editing === "create") {
        await createCard(fields);
        reload(); // a create changes the list, not one card — refetch
      } else if (editing) {
        // Edits of an existing card go through the same optimistic path as a
        // board move, so a rejected PATCH rolls the row back.
        await mutate(editing, fields);
      }
      setEditing(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(card: Card) {
    setBusy(true);
    try {
      await deleteCard(card.card_key);
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Backlog</h1>
        <p>
          Every planning card, highest priority first. Create, edit, move, reprioritise and push a
          card into plan, code or test here — the same cards laid out by status live on the Planning
          board.
        </p>
      </div>

      <CardFilterBar query={query} onQuery={setQuery} filters={filters} onFilters={setFilters}>
        <button
          className="primary-btn"
          onClick={() => {
            open("create");
          }}
          disabled={editing === "create"}
        >
          + New card
        </button>
      </CardFilterBar>

      <CardBanners error={error} notice={notice} />

      {editing === "create" && (
        <CardForm
          draft={draft}
          onDraft={setDraft}
          busy={busy}
          onSave={() => {
            void save();
          }}
          onCancel={() => {
            setEditing(null);
          }}
        />
      )}

      {loading ? (
        <p className="col-empty">Loading cards…</p>
      ) : shown.length === 0 ? (
        <p className="col-empty">
          {cards.length === 0
            ? "No cards yet — create the first one."
            : "Nothing matches the filter."}
        </p>
      ) : (
        <div className="backlog-list">
          {shown.map((card) =>
            editing === card.card_key ? (
              <CardForm
                key={card.card_key}
                draft={draft}
                onDraft={setDraft}
                busy={busy}
                onSave={() => {
                  void save();
                }}
                onCancel={() => {
                  setEditing(null);
                }}
              />
            ) : (
              <CardBody
                key={card.card_key}
                card={card}
                busy={busy}
                issueHref={hrefOf(card.issue_ref)}
                onMutate={(patch) => {
                  void mutate(card.card_key, patch);
                }}
                onStage={(action) => {
                  void runStage(card.card_key, action);
                }}
                onEdit={() => {
                  open(card);
                }}
                onDelete={() => {
                  void remove(card);
                }}
              />
            ),
          )}
        </div>
      )}
    </>
  );
}

// The create/edit form — one component for both, since the fields are identical.
function CardForm({
  draft,
  onDraft,
  busy,
  onSave,
  onCancel,
}: {
  draft: Draft;
  onDraft: (d: Draft) => void;
  busy: boolean;
  onSave: () => void;
  onCancel: () => void;
}) {
  const set = (k: keyof Draft, v: string) => {
    onDraft({ ...draft, [k]: v });
  };
  return (
    <form
      className="card-form"
      onSubmit={(e) => {
        e.preventDefault();
        onSave();
      }}
    >
      <input
        className="card-input"
        placeholder="Title"
        value={draft.title}
        autoFocus
        onChange={(e) => {
          set("title", e.target.value);
        }}
        aria-label="Card title"
      />
      <textarea
        className="card-input card-textarea"
        placeholder="Acceptance criteria — one per line"
        rows={3}
        value={draft.acceptance}
        onChange={(e) => {
          set("acceptance", e.target.value);
        }}
        aria-label="Acceptance criteria"
      />
      <div className="card-form__row">
        <select
          className="card-select"
          value={draft.status}
          onChange={(e) => {
            set("status", e.target.value);
          }}
          aria-label="Status"
        >
          {CARD_STATUSES.map((s) => (
            <option key={s.key} value={s.key}>
              {s.label}
            </option>
          ))}
        </select>
        <select
          className="card-select"
          value={draft.tier}
          onChange={(e) => {
            set("tier", e.target.value);
          }}
          aria-label="Tier"
        >
          <option value="">no tier</option>
          {CARD_TIERS.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <input
          className="card-input card-input--sm"
          type="number"
          min={0}
          placeholder="priority"
          value={draft.priority}
          onChange={(e) => {
            set("priority", e.target.value);
          }}
          aria-label="Priority"
        />
        <input
          className="card-input card-input--sm"
          placeholder="assignee"
          value={draft.assignee}
          onChange={(e) => {
            set("assignee", e.target.value);
          }}
          aria-label="Assignee"
        />
        <input
          className="card-input card-input--sm"
          placeholder="milestone"
          value={draft.milestone}
          onChange={(e) => {
            set("milestone", e.target.value);
          }}
          aria-label="Milestone"
        />
      </div>
      <div className="card-form__actions">
        <button className="primary-btn" type="submit" disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </button>
        <button className="card-btn" type="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  );
}
