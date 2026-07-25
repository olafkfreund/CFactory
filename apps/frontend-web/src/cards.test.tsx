import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CardSchema, fetchCards, patchCard, type Card, type CardPatch } from "./api";
import { byPriority, matchesQuery, optimisticPatch, replaceCard } from "./cards";
import { CardBody } from "./CardParts";
import BacklogView from "./BacklogView";
import PlanningBoard from "./PlanningBoard";

// RFC-0019 Phase 1 (#302). Same testing shape as the rest of this frontend:
// zod boundary assertions, pure-helper unit tests, a stubbed global fetch for the
// HTTP calls, and react-dom/server static markup for the components (no jsdom).

const CARD: Card = {
  card_key: "FCT-42",
  tenant_id: "default",
  title: "Ship the planning board",
  acceptance_criteria: ["backlog view", "kanban view"],
  status: "ready",
  priority: 2,
  tier: "medium",
  assignee: "olaf",
  milestone: "m1",
  correlation_key: null,
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(impl: (url: string, init?: RequestInit) => Response) {
  const spy = vi.fn((url: string, init?: RequestInit) => Promise.resolve(impl(url, init)));
  vi.stubGlobal("fetch", spy);
  return spy;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("CardSchema (HTTP boundary)", () => {
  it("parses the pinned card contract", () => {
    const parsed = CardSchema.parse(CARD);
    expect(parsed.card_key).toBe("FCT-42");
    expect(parsed.acceptance_criteria).toHaveLength(2);
    expect(parsed.tier).toBe("medium");
  });

  it("accepts the nullable fields as null", () => {
    const parsed = CardSchema.parse({
      ...CARD,
      tier: null,
      assignee: null,
      milestone: null,
      correlation_key: null,
    });
    expect(parsed.tier).toBeNull();
  });

  it("tolerates additive unknown fields (forward-compat)", () => {
    const parsed = CardSchema.parse({ ...CARD, a_field_added_later: 1 });
    expect(parsed.title).toBe(CARD.title);
  });

  it("rejects an out-of-contract status", () => {
    expect(() => CardSchema.parse({ ...CARD, status: "archived" })).toThrow();
  });

  it("rejects a card missing a required field", () => {
    expect(() => CardSchema.parse({ ...CARD, priority: undefined })).toThrow();
  });
});

describe("fetchCards", () => {
  it("passes only the set filters as query params", async () => {
    const spy = stubFetch(() => json({ cards: [CARD] }));
    await fetchCards({ status: "ready", assignee: undefined });
    expect(spy.mock.calls[0][0]).toBe("/api/cards?status=ready");
  });

  it("accepts both the bare-array and {cards} list envelopes", async () => {
    stubFetch(() => json([CARD]));
    expect(await fetchCards()).toHaveLength(1);
    stubFetch(() => json({ cards: [CARD], count: 1 }));
    expect((await fetchCards())[0].card_key).toBe("FCT-42");
  });
});

describe("patchCard", () => {
  it("PATCHes the card and validates the response", async () => {
    const spy = stubFetch(() => json({ ...CARD, status: "in_progress" }));
    const saved = await patchCard("FCT-42", { status: "in_progress" });
    expect(spy.mock.calls[0][1]?.method).toBe("PATCH");
    expect(spy.mock.calls[0][0]).toBe("/api/cards/FCT-42");
    expect(saved.status).toBe("in_progress");
  });

  it("throws the backend detail on a rejected write", async () => {
    stubFetch(() => json({ detail: "card is locked" }, 409));
    await expect(patchCard("FCT-42", { priority: 1 })).rejects.toThrow("card is locked");
  });
});

describe("pure helpers", () => {
  it("orders by priority, lower first, stable on ties", () => {
    const a = { ...CARD, card_key: "FCT-2", priority: 1 };
    const b = { ...CARD, card_key: "FCT-1", priority: 1 };
    const c = { ...CARD, card_key: "FCT-3", priority: 0 };
    expect([a, b, c].sort(byPriority).map((x) => x.card_key)).toEqual(["FCT-3", "FCT-1", "FCT-2"]);
  });

  it("matches the query across key, title, assignee and milestone", () => {
    expect(matchesQuery(CARD, "")).toBe(true);
    expect(matchesQuery(CARD, "fct-42")).toBe(true);
    expect(matchesQuery(CARD, "PLANNING")).toBe(true);
    expect(matchesQuery(CARD, "olaf")).toBe(true);
    expect(matchesQuery(CARD, "nope")).toBe(false);
  });

  it("replaces only the matching card", () => {
    const other = { ...CARD, card_key: "FCT-9" };
    const next = replaceCard([CARD, other], { ...CARD, priority: 9 });
    expect(next[0].priority).toBe(9);
    expect(next[1]).toBe(other);
  });
});

describe("optimisticPatch (move / reprioritise)", () => {
  it("applies the change immediately, then settles on the server's card", async () => {
    const states: Card[][] = [];
    const saved = { ...CARD, status: "done" as const, updated_at: "later" };
    await optimisticPatch(
      [CARD],
      "FCT-42",
      { status: "done" },
      (n) => states.push(n),
      () => Promise.resolve(saved),
    );
    expect(states[0][0].status).toBe("done"); // optimistic, before the response
    expect(states[1][0].updated_at).toBe("later"); // server copy wins
  });

  it("rolls back to the previous list when the PATCH fails", async () => {
    const states: Card[][] = [];
    await expect(
      optimisticPatch(
        [CARD],
        "FCT-42",
        { status: "done" },
        (n) => states.push(n),
        () => Promise.reject(new Error("HTTP 500")),
      ),
    ).rejects.toThrow("HTTP 500");
    expect(states[0][0].status).toBe("done"); // shown optimistically…
    expect(states[states.length - 1][0].status).toBe("ready"); // …then reverted
  });

  it("refuses to patch a card that is not in the list", async () => {
    await expect(
      optimisticPatch([CARD], "FCT-99", { priority: 1 }, () => undefined),
    ).rejects.toThrow("unknown card FCT-99");
  });
});

describe("card components", () => {
  const noop = () => undefined;

  it("renders the card with its move + reprioritise controls", () => {
    const html = renderToStaticMarkup(<CardBody card={CARD} busy={false} onMutate={noop} />);
    expect(html).toContain("FCT-42");
    expect(html).toContain("Ship the planning board");
    expect(html).toContain("2 AC");
    expect(html).toContain("medium");
    expect(html).toContain("Status of FCT-42");
    expect(html).toContain("Raise priority of FCT-42");
    expect(html).toContain("Lower priority of FCT-42");
  });

  it("shows edit/delete only when those handlers are supplied", () => {
    const bare = renderToStaticMarkup(<CardBody card={CARD} busy={false} onMutate={noop} />);
    expect(bare).not.toContain("Delete FCT-42");
    const full = renderToStaticMarkup(
      <CardBody card={CARD} busy={false} onMutate={noop} onEdit={noop} onDelete={noop} />,
    );
    expect(full).toContain("Delete FCT-42");
    expect(full).toContain("Edit");
  });

  it("renders the backlog view shell with its filter bar and create action", () => {
    stubFetch(() => json({ cards: [CARD] }));
    const html = renderToStaticMarkup(<BacklogView reloadSignal={0} />);
    expect(html).toContain("Backlog");
    expect(html).toContain("+ New card");
    expect(html).toContain("Filter by status");
  });

  it("renders every planning status as a board column, without a status filter", () => {
    stubFetch(() => json({ cards: [CARD] }));
    const html = renderToStaticMarkup(<PlanningBoard reloadSignal={0} />);
    expect(html).toContain("Planning board");
    for (const label of ["Backlog", "Ready", "In progress", "Blocked", "Done"]) {
      expect(html).toContain(label);
    }
    // status IS the column here, so the status filter must not be offered
    expect(html).not.toContain("Filter by status");
  });
});

// A CardPatch must stay assignable from the mutable card fields — a compile-time
// guard that the client's patch surface tracks the pinned contract.
const _patch: CardPatch = { status: "blocked", priority: 3, tier: "hard", assignee: null };
void _patch;
