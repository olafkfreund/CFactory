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
  runCardStage,
  type Card,
  type CardFilters,
  type CardPatch,
  type CardStage,
  type CardStatus,
  type CardTier,
  type StageActionResult,
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

/** The pipeline stages, in the order `run` walks them. */
export const CARD_STAGES: CardStage[] = ["plan", "code", "test"];

/** A stage action, or the whole sequence. */
export type StageAction = CardStage | "run";

/** The buttons on a card, in press order. `hint` is the title when enabled. */
export const CARD_STAGE_ACTIONS: { key: StageAction; label: string; hint: string }[] = [
  { key: "plan", label: "Plan", hint: "Decompose this card in PFactory" },
  { key: "code", label: "Code", hint: "Build this card in AIFactory" },
  { key: "test", label: "Test", hint: "Verify the build in TFactory" },
  { key: "run", label: "Run all", hint: "Plan, then code, then test" },
];

/**
 * Why this action cannot be taken right now, or null when it can.
 *
 * A deliberate MIRROR of the backend's precondition rules
 * (`card_intake.refuse_reason`) — the same relationship `taskState.ts` has with
 * `status_taxonomy.py`. The backend is the authority and still refuses with a 409
 * whatever the UI does; this only exists so a button that would be refused is
 * disabled with the reason on it rather than inviting a pointless round trip.
 *
 * Deliberately NOT mirrored: `no_intake_project`, which is deployment state the
 * browser cannot see. That one surfaces as the backend's refusal message.
 */
export function stageBlocker(card: Card, action: StageAction): string | null {
  if (!card.tier) return "set a tier first — it decides what gets sent";
  const runs = card.stage_runs;
  const live = CARD_STAGES.find((s) => runs[s]?.status === "dispatched");
  if (action === "run") {
    if (live) return `${live} is already running`;
  } else {
    if (runs[action]?.status === "dispatched") return `${action} is already running`;
    if (runs[action]?.status === "done") return `${action} has already completed`;
  }
  if (action === "test" && !(card.correlation_key && runs.code?.status === "done")) {
    return "nothing built to verify yet — run code to completion first";
  }
  return null;
}

/**
 * The one-line notice a stage action leaves behind, or null when it just worked.
 *
 * Covers the two things a human needs told even though nothing failed: a stage
 * that was SKIPPED (already complete), and a warning that the action overrode
 * tier routing or skipped planning.
 */
export function stageNotice(cardKey: string, result: StageActionResult): string | null {
  const { skipped, reason, warnings, dispatched, ok } = result.stage;
  if (ok === false) return `${cardKey}: ${reason ?? "dispatch failed"}`;
  if (dispatched === false) return `${cardKey}: ${reason ?? skipped ?? "nothing to do"}`;
  return warnings?.length ? `${cardKey}: ${warnings.join(" ")}` : null;
}

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
  /** A non-failure thing the human should know: a skipped stage, a warning. */
  notice: string | null;
  /** Move / reprioritise / edit one card, optimistically with rollback. */
  mutate: (cardKey: string, patch: CardPatch) => Promise<void>;
  /** Push one card into a stage, or through the whole sequence (RFC-0020 §3.7). */
  runStage: (cardKey: string, action: StageAction) => Promise<void>;
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
  const [notice, setNotice] = useState<string | null>(null);
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

  // Not optimistic, unlike `mutate`: a dispatch's outcome is not knowable in
  // advance (it may be refused, skipped, or blocked upstream), so the card is
  // replaced only with what the server actually reports.
  const runStage = useCallback(async (cardKey: string, action: StageAction) => {
    try {
      const result = await runCardStage(cardKey, action);
      if (result.card) {
        const settled = result.card;
        setCards((current) => replaceCard(current, settled));
      }
      setError(null);
      setNotice(stageNotice(cardKey, result));
    } catch (e) {
      setNotice(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const reload = useCallback(() => {
    setTick((t) => t + 1);
  }, []);
  return { cards, loading, error, notice, mutate, runStage, reload, setError };
}
