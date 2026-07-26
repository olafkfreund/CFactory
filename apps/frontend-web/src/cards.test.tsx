import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CardImportSchema,
  CardSchema,
  CardSyncStateSchema,
  fetchCardSyncState,
  fetchCards,
  GitConnectionsSchema,
  patchCard,
  runCardStage,
  type Card,
  type CardPatch,
} from "./api";
import {
  byPriority,
  importNotice,
  issueUrl,
  issueUrlResolver,
  matchesQuery,
  peek,
  optimisticPatch,
  replaceCard,
  relativeAge,
  stageBlocker,
  stageNotice,
  syncSummary,
} from "./cards";
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
  labels: [],
  stage_runs: {},
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

// ── Stage actions (RFC-0020 §3.7, #369) ─────────────────────────────────────

const BUILT: Card = {
  ...CARD,
  correlation_key: "task-7",
  stage_runs: { code: { service: "aifactory", status: "done" } },
};

describe("runCardStage", () => {
  it("POSTs to the named stage action and validates the response", async () => {
    const spy = stubFetch(() => json({ stage: { dispatched: true, stage: "code" }, card: CARD }));
    const result = await runCardStage("FCT-42", "code");
    expect(spy.mock.calls[0][0]).toBe("/api/cards/FCT-42/actions/code");
    expect(spy.mock.calls[0][1]?.method).toBe("POST");
    expect(result.stage.dispatched).toBe(true);
  });

  it("posts the sequence to /actions/run", async () => {
    const spy = stubFetch(() =>
      json({ stage: { sequence: ["plan", "code", "test"] }, card: CARD }),
    );
    const result = await runCardStage("FCT-42", "run");
    expect(spy.mock.calls[0][0]).toBe("/api/cards/FCT-42/actions/run");
    expect(result.stage.sequence).toEqual(["plan", "code", "test"]);
  });

  it("surfaces a refusal's human sentence, not just its status code", async () => {
    stubFetch(() =>
      json({ detail: { reason: "no_build_to_verify", message: "nothing built to verify" } }, 409),
    );
    await expect(runCardStage("FCT-42", "test")).rejects.toThrow("nothing built to verify");
  });

  it("still surfaces a plain string detail (every other endpoint's shape)", async () => {
    stubFetch(() => json({ detail: "no card 'FCT-9'" }, 404));
    await expect(runCardStage("FCT-9", "plan")).rejects.toThrow("no card 'FCT-9'");
  });
});

describe("stageBlocker (the backend's preconditions, mirrored for the UI)", () => {
  it("blocks everything on a card with no tier", () => {
    const untiered = { ...CARD, tier: null };
    for (const action of ["plan", "code", "test", "run"] as const) {
      expect(stageBlocker(untiered, action)).toMatch(/tier/);
    }
  });

  it("blocks test until a build has completed", () => {
    expect(stageBlocker(CARD, "test")).toMatch(/nothing built/);
    const running = {
      ...CARD,
      correlation_key: "task-7",
      stage_runs: { code: { status: "dispatched" as const } },
    };
    expect(stageBlocker(running, "test")).toMatch(/nothing built/);
    expect(stageBlocker(BUILT, "test")).toBeNull();
  });

  it("blocks a stage that is already running, and only that stage", () => {
    const planning = { ...CARD, stage_runs: { plan: { status: "dispatched" as const } } };
    expect(stageBlocker(planning, "plan")).toMatch(/already running/);
    expect(stageBlocker(planning, "code")).toBeNull();
    // ...but `run` is blocked by ANY live stage: the sequence is already in motion.
    expect(stageBlocker(planning, "run")).toMatch(/already running/);
  });

  it("blocks a completed stage (pressing it would be a no-op)", () => {
    expect(stageBlocker(BUILT, "code")).toMatch(/already completed/);
    expect(stageBlocker(BUILT, "run")).toBeNull(); // run resumes at the next stage
  });

  it("allows plan on a low/medium card — the override is the point", () => {
    expect(stageBlocker({ ...CARD, tier: "low" }, "plan")).toBeNull();
  });
});

describe("stageNotice", () => {
  const result = (stage: Record<string, unknown>) => ({ stage, card: CARD });

  it("says nothing when a dispatch simply worked", () => {
    expect(stageNotice("FCT-42", result({ dispatched: true, ok: true }))).toBeNull();
  });

  it("reports a skipped stage", () => {
    const notice = stageNotice(
      "FCT-42",
      result({
        dispatched: false,
        ok: true,
        skipped: "stage_already_complete",
        reason: "the code stage has already completed",
      }),
    );
    expect(notice).toContain("FCT-42");
  });

  it("reports a failed dispatch even though the request itself succeeded", () => {
    expect(
      stageNotice("FCT-42", result({ ok: false, reason: "dispatch failed: HTTP 500" })),
    ).toContain("dispatch failed");
  });

  it("passes warnings through so an override is never silent", () => {
    const notice = stageNotice(
      "FCT-42",
      result({ dispatched: true, ok: true, warnings: ["plan_skipped: tier 'hard' …"] }),
    );
    expect(notice).toContain("plan_skipped");
  });
});

describe("stage action buttons", () => {
  const noop = () => undefined;

  it("renders all four buttons only when a stage handler is supplied", () => {
    const bare = renderToStaticMarkup(<CardBody card={CARD} busy={false} onMutate={noop} />);
    expect(bare).not.toContain("Plan FCT-42");
    const staged = renderToStaticMarkup(
      <CardBody card={CARD} busy={false} onMutate={noop} onStage={noop} />,
    );
    for (const label of ["Plan FCT-42", "Code FCT-42", "Test FCT-42", "Run all FCT-42"]) {
      expect(staged).toContain(label);
    }
  });

  it("disables a blocked action and puts the reason on it", () => {
    const html = renderToStaticMarkup(
      <CardBody card={CARD} busy={false} onMutate={noop} onStage={noop} />,
    );
    // No build yet, so Test must be unpressable with the reason visible.
    expect(html).toMatch(/nothing built to verify yet[^>]*disabled|disabled[^>]*nothing built/);
  });

  it("shows each stage's dispatch record so the state is readable", () => {
    const html = renderToStaticMarkup(
      <CardBody card={BUILT} busy={false} onMutate={noop} onStage={noop} />,
    );
    expect(html).toContain("code · done");
  });
});

// A CardPatch must stay assignable from the mutable card fields — a compile-time
// guard that the client's patch surface tracks the pinned contract.
const _patch: CardPatch = { status: "blocked", priority: 3, tier: "hard", assignee: null };
void _patch;

// ── Imported issue body + metadata (#213) ───────────────────────────────────
// A card whose body came from a real issue: markdown with a heading, a task list,
// a fenced code block and a link — the four things an issue body actually
// contains and the four the bare-title board threw away.
const BODY = [
  "## Why",
  "",
  "The board drops the body. See [the issue](https://github.com/acme/widgets/issues/7).",
  "",
  "- [x] import the body",
  "- [ ] render the body",
  "",
  "```python",
  "def go(x: int) -> int:  # *not* italics",
  "    return x",
  "```",
].join("\n");

const IMPORTED: Card = {
  ...CARD,
  description: BODY,
  issue_ref: "acme/widgets#7",
  issue_state: "open",
  labels: ["bug", "frontend"],
};

describe("issueUrl (multi-provider link out)", () => {
  it("maps the github API root back to the web host", () => {
    expect(issueUrl("acme/widgets#7", "github", "https://api.github.com")).toBe(
      "https://github.com/acme/widgets/issues/7",
    );
  });

  it("keeps a GitHub Enterprise host and drops its /api/v3 suffix", () => {
    expect(issueUrl("acme/widgets#7", "github", "https://ghe.corp/api/v3")).toBe(
      "https://ghe.corp/acme/widgets/issues/7",
    );
  });

  it("uses GitLab's own issue path, on the configured host", () => {
    expect(issueUrl("grp/sub/proj#12", "gitlab", "https://gitlab.corp")).toBe(
      "https://gitlab.corp/grp/sub/proj/-/issues/12",
    );
  });

  it("returns null rather than a wrong URL for a provider it cannot address", () => {
    expect(issueUrl("org/proj/repo#12", "azure_devops", "https://dev.azure.com")).toBeNull();
  });

  it("returns null for a missing or malformed ref", () => {
    expect(issueUrl(null, "github", "https://api.github.com")).toBeNull();
    expect(issueUrl(undefined, "github", "https://api.github.com")).toBeNull();
    expect(issueUrl("acme/widgets", "github", "https://api.github.com")).toBeNull();
  });
});

// A board holds cards across SEVERAL connections now, so the link out is resolved
// through the card's own repository (#373). One tenant-wide host would hand a
// GitLab card a github.com URL, which is the failure this replaces.
const CONNECTIONS = GitConnectionsSchema.parse({
  connections: [
    {
      id: 1,
      tenant_id: "acme",
      provider: "github",
      base_url: "https://api.github.com",
      label: "Work GitHub",
      status: "verified",
      repositories: [
        { id: 11, connection_id: 1, tenant_id: "acme", project: "acme/widgets", is_default: true },
      ],
    },
    {
      id: 2,
      tenant_id: "acme",
      provider: "gitlab",
      base_url: "https://gitlab.corp",
      label: "self-hosted GitLab",
      status: "configured",
      repositories: [
        { id: 21, connection_id: 2, tenant_id: "acme", project: "grp/sub/proj" },
      ],
    },
  ],
  default_repository_id: 11,
});

describe("issueUrlResolver (the link out follows the card's repository)", () => {
  const resolve = issueUrlResolver(CONNECTIONS);

  it("sends a card on connection A and a card on connection B to DIFFERENT hosts", () => {
    const onGithub = resolve({ ...CARD, repository_id: 11, issue_ref: "acme/widgets#7" });
    const onGitlab = resolve({ ...CARD, repository_id: 21, issue_ref: "grp/sub/proj#12" });
    expect(onGithub).toBe("https://github.com/acme/widgets/issues/7");
    expect(onGitlab).toBe("https://gitlab.corp/grp/sub/proj/-/issues/12");
    expect(onGithub).not.toBe(onGitlab);
  });

  it("resolves a card that names no repository through the tenant default", () => {
    expect(resolve({ ...CARD, issue_ref: "acme/widgets#7" })).toBe(
      "https://github.com/acme/widgets/issues/7",
    );
    expect(resolve({ ...CARD, repository_id: null, issue_ref: "acme/widgets#7" })).toBe(
      "https://github.com/acme/widgets/issues/7",
    );
  });

  it("falls back to the default when the named repository is gone, as the backend does", () => {
    expect(resolve({ ...CARD, repository_id: 999, issue_ref: "acme/widgets#7" })).toBe(
      "https://github.com/acme/widgets/issues/7",
    );
  });

  it("gives a card no link at all rather than a wrong one when nothing is configured", () => {
    const unknown = issueUrlResolver(null);
    expect(unknown({ ...CARD, issue_ref: "acme/widgets#7" })).toBeNull();
    const empty = issueUrlResolver(
      GitConnectionsSchema.parse({ connections: [], default_repository_id: null }),
    );
    expect(empty({ ...CARD, issue_ref: "acme/widgets#7" })).toBeNull();
  });

  it("still returns null for a provider whose URL shape is not derivable", () => {
    const azure = issueUrlResolver(
      GitConnectionsSchema.parse({
        connections: [
          {
            id: 3,
            tenant_id: "acme",
            provider: "azure_devops",
            base_url: "https://dev.azure.com",
            label: "Azure DevOps",
            status: "configured",
            repositories: [
              { id: 31, connection_id: 3, tenant_id: "acme", project: "org/proj/repo", is_default: true },
            ],
          },
        ],
        default_repository_id: 31,
      }),
    );
    expect(azure({ ...CARD, repository_id: 31, issue_ref: "org/proj/repo#12" })).toBeNull();
  });
});

describe("peek (the collapsed one-liner)", () => {
  it("flattens markdown to one short line and drops fenced code", () => {
    const line = peek(BODY);
    expect(line).not.toContain("\n");
    expect(line).not.toContain("```");
    expect(line).not.toContain("def go");
    expect(line).toContain("The board drops the body");
    expect(line.length).toBeLessThanOrEqual(161); // 160 + the ellipsis
  });

  it("never renders as blank, even for a body that is only code", () => {
    expect(peek("```\nx = 1\n```")).toBe("Issue body");
  });
});

describe("card body rendering (#213)", () => {
  const noop = () => undefined;
  const render = (card: Card, href?: string | null) =>
    renderToStaticMarkup(
      <CardBody card={card} busy={false} issueHref={href} onMutate={noop} />,
    );

  it("renders the issue body as markdown, not as raw text", () => {
    const html = render(IMPORTED);
    expect(html).toContain("<details");
    expect(html).toContain("md-h2"); // "## Why" became a heading
    expect(html).toContain("md-pre"); // the fence became a code block
    expect(html).toContain('type="checkbox"'); // the task list became checkboxes
    expect(html).toContain("def go(x: int) -&gt; int:  # *not* italics"); // code stays literal
    expect(html).toContain('href="https://github.com/acme/widgets/issues/7"');
  });

  it("shows the collapsed peek and keeps the full body behind the disclosure", () => {
    const html = render(IMPORTED);
    expect(html).toContain("card-pl__peek-text");
    // <details> is closed by default — the list stays one line per card.
    expect(html).not.toContain("<details open");
  });

  it("renders nothing at all for an empty or null body", () => {
    const noBody = { ...IMPORTED, description: null, acceptance_criteria: [] };
    expect(render(noBody)).not.toContain("<details");
    const blank = { ...noBody, description: "   \n  " };
    expect(render(blank)).not.toContain("<details");
    // …and the card itself still renders.
    expect(render(noBody)).toContain("Ship the planning board");
  });

  it("still offers the disclosure for acceptance criteria when there is no body", () => {
    const html = render({ ...IMPORTED, description: null });
    expect(html).toContain("<details");
    expect(html).toContain("2 acceptance criteria");
    expect(html).toContain("backlog view");
  });

  it("renders the issue ref as a link when the host is known", () => {
    const html = render(IMPORTED, "https://github.com/acme/widgets/issues/7");
    expect(html).toContain('href="https://github.com/acme/widgets/issues/7"');
    expect(html).toContain("acme/widgets#7");
    expect(html).toContain('rel="noreferrer noopener"');
  });

  it("renders the issue ref as plain text when the host is not known", () => {
    // No body here: a link INSIDE a body would put an <a> on the card either way,
    // and what is under test is the metadata chip.
    const html = render({ ...IMPORTED, description: null, acceptance_criteria: [] }, null);
    expect(html).toContain("acme/widgets#7");
    expect(html).not.toContain("card-chip--issue");
    expect(html).not.toContain("<a ");
  });

  it("shows the mirrored issue state and labels", () => {
    const html = render(IMPORTED);
    expect(html).toContain("card-chip--state-open");
    expect(html).toContain("bug");
    expect(html).toContain("frontend");
  });

  it("refuses to make a javascript: URL in a body clickable", () => {
    const html = render({
      ...IMPORTED,
      description: "[click me](javascript:alert(1))",
    });
    expect(html).not.toContain("javascript:");
    expect(html).toContain("click me");
  });

  it("renders HTML in a body as visible text, never as markup", () => {
    const html = render({ ...IMPORTED, description: "<img src=x onerror=alert(1)>" });
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });
});


// ── the board says how current it is (#374) ─────────────────────────────────

describe("sync freshness (#374)", () => {
  const STATE = {
    now: "2026-07-26T12:00:00Z",
    poll: { enabled: true, interval_seconds: 300, live: false },
    repositories: [
      {
        repository_id: 1,
        project: "acme/widgets",
        is_default: true,
        last_polled_at: "2026-07-26T11:58:00Z",
        watermark_at: "2026-07-20T10:00:00Z",
        stale: false,
      },
    ],
  };
  const NOW = Date.parse("2026-07-26T12:00:00Z");

  it("parses the sync-state payload at the HTTP boundary", () => {
    const parsed = CardSyncStateSchema.parse(STATE);
    expect(parsed.repositories[0].stale).toBe(false);
    expect(parsed.poll.live).toBe(false);
  });

  it("fetches it from the literal path, not as a card key", async () => {
    const spy = stubFetch(() => json(STATE));
    await fetchCardSyncState();
    expect(spy.mock.calls[0][0]).toBe("/api/cards/sync-state");
  });

  it("renders an age a human can read, not an ISO timestamp", () => {
    expect(relativeAge("2026-07-26T11:58:00Z", NOW)).toBe("2 min ago");
    expect(relativeAge("2026-07-26T11:59:40Z", NOW)).toBe("just now");
    expect(relativeAge("2026-07-26T09:00:00Z", NOW)).toBe("3 h ago");
    expect(relativeAge("2026-07-22T12:00:00Z", NOW)).toBe("4 d ago");
    expect(relativeAge(null, NOW)).toBe("never");
  });

  it("says the board is current, and that it is a poll rather than live", () => {
    const summary = syncSummary(STATE, NOW);
    expect(summary).toContain("Synced 2 min ago");
    expect(summary).toContain("polls every 5 min");
    expect(summary).toContain("not live");
    expect(summary).not.toContain("STALE");
  });

  it("says STALE when every repository has gone unread — the invisible failure", () => {
    const stale = {
      ...STATE,
      repositories: [{ ...STATE.repositories[0], last_polled_at: null, stale: true }],
    };
    const summary = syncSummary(stale, NOW);
    expect(summary).toContain("Synced never");
    expect(summary).toContain("STALE");
  });

  it("names the ONE lagging repository rather than crying stale for the board", () => {
    const mixed = {
      ...STATE,
      repositories: [
        STATE.repositories[0],
        { ...STATE.repositories[0], repository_id: 2, project: "acme/gadgets", stale: true },
      ],
    };
    expect(syncSummary(mixed, NOW)).toContain("1 of 2 repositories are stale");
  });

  it("says the poll is OFF, because no timestamp means the board will catch up", () => {
    const off = { ...STATE, poll: { ...STATE.poll, enabled: false } };
    const summary = syncSummary(off, NOW);
    expect(summary).toContain("automatic sync is OFF");
    expect(summary).toContain("Sync now");
  });

  it("says so when there is nothing connected to sync with", () => {
    expect(syncSummary({ ...STATE, repositories: [] }, NOW)).toContain("No repository connected");
  });

  it("degrades quietly when the staleness read itself fails", () => {
    // Not knowing how fresh the board is must not look like the board being broken.
    expect(syncSummary(null, NOW)).toBe("Sync state unavailable");
  });

  it("reports the READ time in the import notice, not the incremental cursor", () => {
    const notice = importNotice(
      CardImportSchema.parse({
        ok: true,
        project: "acme/widgets",
        imported: 3,
        updated: 1,
        skipped: 0,
        truncated: false,
        last_synced_at: "2026-07-20T09:59:00Z",
        polled_at: "2026-07-26T11:58:00Z",
      }),
    );
    expect(notice).toContain("Imported 3, updated 1");
    expect(notice).toContain("Synced 2026-07-26T11:58:00Z");
  });

  it("puts the sync line and a Sync now control on the planning board", () => {
    stubFetch((url) =>
      url.includes("sync-state") ? json(STATE) : json({ count: 0, cards: [] }),
    );
    const html = renderToStaticMarkup(<PlanningBoard reloadSignal={0} />);
    expect(html).toContain("card-sync");
    expect(html).toContain("Sync now");
  });
});
