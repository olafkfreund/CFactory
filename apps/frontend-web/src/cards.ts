// Shared planning-card logic (RFC-0019 Phase 1, #302) used by BOTH the Backlog
// list and the planning Kanban board: the status vocabulary, the pure
// ordering/filter helpers, the optimistic-mutation primitive and the `useCards`
// data hook. The chrome the two views share lives in CardParts.tsx; everything
// cross-view lives in one of those two so neither view copies the other.
//
// Planning statuses are NOT the PARR pipeline stage statuses rendered by
// Board.tsx: that board is execution (plan → code → test); this one is the
// planning axis (backlog → done). They are deliberately separate surfaces.
import { useCallback, useEffect, useState } from "react";
import {
  fetchCards,
  patchCard,
  type Card,
  type CardFilters,
  type CardPatch,
  type CardStatus,
  type CardTier,
} from "./api";

/** The Kanban columns, in board order. `cls` picks the column accent in CSS. */
export const CARD_STATUSES: { key: CardStatus; label: string; cls: string }[] = [
  { key: "backlog", label: "Backlog", cls: "backlog" },
  { key: "ready", label: "Ready", cls: "ready" },
  { key: "in_progress", label: "In progress", cls: "progress" },
  { key: "blocked", label: "Blocked", cls: "blocked" },
  { key: "done", label: "Done", cls: "done" },
];

export const CARD_TIERS: CardTier[] = ["low", "medium", "hard"];

/** Priority order: lower number first, then card_key so the sort is stable. */
export function byPriority(a: Card, b: Card): number {
  return a.priority - b.priority || a.card_key.localeCompare(b.card_key);
}

/** Case-insensitive match over the fields a human would search by. */
export function matchesQuery(card: Card, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [card.card_key, card.title, card.assignee, card.milestone, card.correlation_key]
    .filter((v): v is string => Boolean(v))
    .some((v) => v.toLowerCase().includes(q));
}

/** Replace one card in the list by key (identity-preserving for the others). */
export function replaceCard(cards: Card[], next: Card): Card[] {
  return cards.map((c) => (c.card_key === next.card_key ? next : c));
}

/**
 * Optimistically apply `patch` to one card, PATCH it, then settle on the
 * server's copy — rolling the list back to `cards` if the request fails.
 *
 * Written as a plain function over (list, setter) rather than inside the hook so
 * the rollback contract is unit-testable without a DOM: a failed PATCH must
 * never leave the UI showing a move that did not happen. Rethrows so the caller
 * can surface the backend's message.
 */
export async function optimisticPatch(
  cards: Card[],
  cardKey: string,
  patch: CardPatch,
  apply: (next: Card[]) => void,
  send: (key: string, p: CardPatch) => Promise<Card> = patchCard,
): Promise<Card> {
  const current = cards.find((c) => c.card_key === cardKey);
  if (!current) throw new Error(`unknown card ${cardKey}`);
  apply(replaceCard(cards, { ...current, ...patch }));
  try {
    const saved = await send(cardKey, patch);
    apply(replaceCard(cards, saved));
    return saved;
  } catch (e) {
    apply(cards); // roll back — the optimistic edit never happened
    throw e;
  }
}

export type CardsState = {
  cards: Card[];
  loading: boolean;
  error: string | null;
  /** Move / reprioritise / edit one card, optimistically with rollback. */
  mutate: (cardKey: string, patch: CardPatch) => Promise<void>;
  /** Re-run the list query (after a create/delete). */
  reload: () => void;
  setError: (message: string | null) => void;
};

/**
 * Load the card list for `filters` and expose the optimistic mutation. Follows
 * the ServicesView fetch convention (effect + alive guard + reloadSignal).
 */
export function useCards(filters: CardFilters, reloadSignal: number): CardsState {
  const [cards, setCards] = useState<Card[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  // Serialise the filter object so the effect re-runs on value (not identity).
  const filterKey = JSON.stringify(filters);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchCards(JSON.parse(filterKey) as CardFilters)
      .then((list) => {
        if (alive) {
          setCards(list);
          setError(null);
        }
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [filterKey, reloadSignal, tick]);

  const mutate = useCallback(
    async (cardKey: string, patch: CardPatch) => {
      try {
        await optimisticPatch(cards, cardKey, patch, setCards);
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [cards],
  );

  const reload = useCallback(() => {
    setTick((t) => t + 1);
  }, []);
  return { cards, loading, error, mutate, reload, setError };
}
