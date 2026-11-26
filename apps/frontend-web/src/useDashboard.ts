// The cockpit's data + notification plumbing, extracted from App.tsx (#115):
// work-item state, live progress, the WebSocket feed, the toast/pin/alarm
// notification machine, and the refresh/load actions. App becomes a thin
// composition root that consumes this hook. Pure extraction — the logic, effect
// ordering and dependency arrays are unchanged.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchCards,
  fetchGitConnections,
  fetchHealth,
  fetchProgress,
  fetchProjects,
  fetchSettings,
  fetchWorkItems,
  openFeed,
  refresh as apiRefresh,
  resolveProject,
  type Card,
  type GitConnections,
  type LiveProgress,
  type ServerProject,
  type WorkItem,
} from "./api";
import { onReopen } from "./onReopen";
import { diffEvents } from "./taskEvents";
import { ensureNotifyPermission, osNotify, type TaskEvent } from "./notify";
import { stageState, type TaskState } from "./taskState";
import type { StripAlarm, StripStage } from "./pipelineStages";
import type { TickerPin } from "./EventTicker";
import { TICKER_PREF, type Backend, type View } from "./dashboard";
import type { BreadcrumbProject } from "./ProjectBreadcrumb";
import {
  buildCockpitPath,
  filterWorkItemsForProject,
  parseCockpitRoute,
  type CockpitProject,
} from "./projectRoute";

function toCockpitProject(project: ServerProject): CockpitProject {
  return {
    id: project.id,
    name: project.name,
    slug: project.slug,
    createdAt: project.created_at,
    repositoryIds: project.repository_ids,
    repoPaths: project.repo_paths,
  };
}

export interface ToastEntry {
  id: number;
  ev: TaskEvent;
}

export interface Dashboard {
  backend: Backend;
  items: WorkItem[];
  view: View;
  setView: (v: View) => void;
  projectRef: string | null;
  activeProject: CockpitProject | null;
  activeProjectSlug: string | undefined;
  projects: CockpitProject[];
  breadcrumbProjects: BreadcrumbProject[];
  connectionsLoaded: boolean;
  connectionsError: string | null;
  defaultRepositoryId: number | null | undefined;
  setProjectSlug: (slug: string | null) => void;
  busy: boolean;
  live: boolean;
  error: string | null;
  tick: number;
  progress: Record<string, LiveProgress>;
  toasts: ToastEntry[];
  copilotOpen: boolean;
  setCopilotOpen: React.Dispatch<React.SetStateAction<boolean>>;
  stageFilter: StripStage | null;
  setStageFilter: React.Dispatch<React.SetStateAction<StripStage | null>>;
  pins: TickerPin[];
  alarm: StripAlarm | null;
  tickerOpen: boolean;
  dismissToast: (id: number) => void;
  unpin: (id: number) => void;
  toggleTicker: () => void;
  onSelectStage: (stage: StripStage | null) => void;
  onRefresh: () => Promise<void>;
  retryProjects: () => Promise<void>;
}

export function initialRouteState(pathname: string): {
  view: View;
  projectRef: string | null;
  redirectTo: string | null;
} {
  const route = parseCockpitRoute(pathname);
  if (route.kind === "project") {
    return { view: route.view, projectRef: route.ref, redirectTo: null };
  }
  if (route.kind === "all") {
    return { view: route.view, projectRef: null, redirectTo: null };
  }
  const target = parseCockpitRoute(route.to);
  return {
    view: target.kind === "redirect" ? "overview" : target.view,
    projectRef: target.kind === "project" ? target.ref : null,
    redirectTo: route.to,
  };
}

export function useDashboard(): Dashboard {
  const [backend, setBackend] = useState<Backend>({ kind: "loading" });
  const [allItems, setAllItems] = useState<WorkItem[]>([]);
  const [cards, setCards] = useState<Card[]>([]);
  const [connections, setConnections] = useState<GitConnections | null>(null);
  const [projects, setProjects] = useState<CockpitProject[]>([]);
  const [tenant, setTenant] = useState<string>("default");
  const [resolved, setResolved] = useState<{
    tenant: string;
    ref: string;
    project: CockpitProject | null;
    error: string | null;
  } | null>(null);
  const [connectionsLoaded, setConnectionsLoaded] = useState(false);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [routeState, setRouteState] = useState(() => initialRouteState(window.location.pathname));
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [progress, setProgress] = useState<Record<string, LiveProgress>>({});
  const [toasts, setToasts] = useState<ToastEntry[]>([]);
  const [copilotOpen, setCopilotOpen] = useState(false);
  const [stageFilter, setStageFilter] = useState<StripStage | null>(null);
  const [pins, setPins] = useState<TickerPin[]>([]);
  const [alarm, setAlarm] = useState<StripAlarm | null>(null);
  const [tickerOpen, setTickerOpen] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(TICKER_PREF) !== "0";
    } catch {
      return true;
    }
  });

  // Notification plumbing: remember each task's last overall state so we only
  // alert on genuine transitions, and seed the baseline silently on first load.
  const statesRef = useRef<Map<string, TaskState>>(new Map());
  const seededRef = useRef(false);
  const toastIdRef = useRef(0);
  const pinIdRef = useRef(0);
  const alarmTickRef = useRef(0);
  const projectsSeqRef = useRef(0);
  const scopeRef = useRef<{
    ref: string | null;
    project: CockpitProject | null;
    projects: CockpitProject[];
    cards: Card[];
    defaultRepoId: number | null | undefined;
  }>({ ref: null, project: null, projects: [], cards: [], defaultRepoId: null });
  const [alarmKey, setAlarmKey] = useState<string | null>(null);

  const ingest = useCallback((incoming: WorkItem[]) => {
    const events = diffEvents(statesRef.current, incoming, seededRef.current).filter((ev) => {
      const scope = scopeRef.current;
      if (scope.ref === null) return true;
      return (
        scope.project !== null &&
        filterWorkItemsForProject(
          incoming,
          scope.project,
          scope.cards,
          scope.projects,
          scope.defaultRepoId,
        ).some((item) => item.correlation_key === ev.key)
      );
    });
    if (!seededRef.current) {
      seededRef.current = true;
      return; // first hydrate is not "news"
    }
    if (events.length === 0) return;
    for (const ev of events) osNotify(ev);

    // Failures take over the frame: pin them in the ticker, flare the affected
    // pipeline-strip node, and run the one-shot border sweep (CSS, PRM-gated).
    const failures = events.filter((ev) => ev.kind === "failed");
    if (failures.length > 0) {
      setPins((prev) =>
        [
          ...failures.map((ev) => ({
            id: ++pinIdRef.current,
            key: ev.key,
            title: ev.title,
            at: Date.now(),
          })),
          ...prev,
        ].slice(0, 4),
      );
      const latest = failures.at(-1);
      setAlarmKey(latest?.key ?? null);
      const wi = incoming.find((w) => w.correlation_key === latest?.key);
      const stage = wi
        ? (["tfactory", "aifactory", "pfactory"] as const).find(
            (k) => stageState(wi[k].status) === "failed",
          )
        : undefined;
      setAlarm({ stage: stage ?? "aifactory", tick: ++alarmTickRef.current });
    }

    setToasts((prev) => {
      const next = [...prev];
      for (const ev of events) next.push({ id: ++toastIdRef.current, ev });
      // Failure toasts persist until dismissed; keep the rest of the stack short.
      const failed = next.filter((t) => t.ev.kind === "failed").slice(-4);
      const others = next.filter((t) => t.ev.kind !== "failed").slice(-4);
      return [...failed, ...others].sort((a, b) => a.id - b.id);
    });
  }, []);

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const unpin = useCallback((id: number) => {
    setPins((prev) => prev.filter((p) => p.id !== id));
  }, []);

  const toggleTicker = useCallback(() => {
    setTickerOpen((open) => {
      try {
        window.localStorage.setItem(TICKER_PREF, open ? "0" : "1");
      } catch {
        /* private mode */
      }
      return !open;
    });
  }, []);

  const breadcrumbProjects = useMemo<BreadcrumbProject[]>(
    () =>
      projects.map((p) => ({
        slug: p.slug ?? p.id,
        name: p.name,
      })),
    [projects],
  );
  const resolvedForRoute =
    routeState.projectRef !== null &&
    resolved?.tenant === tenant &&
    (resolved.ref === routeState.projectRef ||
      (resolved.project !== null &&
        (resolved.project.slug ?? resolved.project.id) === routeState.projectRef))
      ? resolved
      : null;
  const activeProject = useMemo(
    () => (resolvedForRoute ? resolvedForRoute.project : null),
    [resolvedForRoute],
  );
  const activeProjectSlug = useMemo(
    () => (activeProject ? (activeProject.slug ?? activeProject.id) : undefined),
    [activeProject],
  );
  const effectiveConnectionsError =
    connectionsError ?? (routeState.projectRef !== null ? (resolvedForRoute?.error ?? null) : null);
  const projectsReady =
    connectionsLoaded &&
    (routeState.projectRef === null || resolvedForRoute !== null || connectionsError !== null);
  scopeRef.current = {
    ref: routeState.projectRef,
    project: activeProject,
    projects,
    cards,
    defaultRepoId: connections?.default_repository_id,
  };
  const items = useMemo(
    () =>
      routeState.projectRef === null
        ? allItems
        : activeProject
          ? filterWorkItemsForProject(
              allItems,
              activeProject,
              cards,
              projects,
              connections?.default_repository_id,
            )
          : [],
    [
      allItems,
      routeState.projectRef,
      activeProject,
      cards,
      projects,
      connections?.default_repository_id,
    ],
  );
  const visibleKeys = new Set(items.map((item) => item.correlation_key));

  useEffect(() => {
    const ref = routeState.projectRef;
    if (!ref) {
      // leaving the project: a later visit asks the server again instead of reusing this outcome
      setResolved(null);
      return;
    }
    if (!connectionsLoaded || connectionsError !== null) {
      return;
    }
    if (resolvedForRoute !== null) return;
    let cancelled = false;
    // a catalog reload bumps the sequence: an outcome from before it must not be written back
    const seq = projectsSeqRef.current;
    resolveProject(tenant, ref)
      .then((res) => {
        if (cancelled || seq !== projectsSeqRef.current) return;
        const nextProject = "project" in res ? toCockpitProject(res.project) : null;
        setResolved({
          tenant,
          ref,
          project: nextProject,
          error: null,
        });
      })
      .catch((err: unknown) => {
        if (cancelled || seq !== projectsSeqRef.current) return;
        setResolved({
          tenant,
          ref,
          project: null,
          error: err instanceof Error ? err.message : String(err),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [routeState.projectRef, connectionsLoaded, connectionsError, resolvedForRoute, tenant]);

  // Sync URL on popstate (browser back/forward).
  useEffect(() => {
    const onPopState = () => {
      setRouteState(initialRouteState(window.location.pathname));
    };
    window.addEventListener("popstate", onPopState);
    return () => {
      window.removeEventListener("popstate", onPopState);
    };
  }, []);

  // Canonicalize redirect routes or non-canonical project refs via replaceState.
  useEffect(() => {
    if (routeState.redirectTo) {
      const targetPath = routeState.redirectTo;
      if (window.location.pathname !== targetPath) {
        window.history.replaceState(
          {},
          "",
          targetPath + window.location.search + window.location.hash,
        );
      }
      setRouteState((prev) => ({ ...prev, redirectTo: null }));
      return;
    }
    if (routeState.projectRef && activeProject) {
      const canonicalSlug = activeProject.slug ?? activeProject.id;
      const canonicalPath = buildCockpitPath(canonicalSlug, routeState.view);
      if (routeState.projectRef !== canonicalSlug || window.location.pathname !== canonicalPath) {
        if (window.location.pathname !== canonicalPath) {
          window.history.replaceState(
            {},
            "",
            canonicalPath + window.location.search + window.location.hash,
          );
        }
        if (routeState.projectRef !== canonicalSlug) {
          setRouteState((prev) => ({ ...prev, projectRef: canonicalSlug }));
        }
      }
    }
  }, [routeState.redirectTo, routeState.projectRef, routeState.view, activeProject]);

  const setView = useCallback(
    (nextView: View) => {
      const slug = ["tokens", "audit", "services", "preferences"].includes(nextView)
        ? null
        : activeProject
          ? (activeProject.slug ?? activeProject.id)
          : routeState.projectRef;
      const nextPath = buildCockpitPath(slug, nextView);
      if (window.location.pathname !== nextPath) {
        window.history.pushState({}, "", nextPath);
      }
      setRouteState({ view: nextView, projectRef: slug, redirectTo: null });
    },
    [activeProject, routeState.projectRef],
  );

  const setProjectSlug = useCallback(
    (nextSlug: string | null) => {
      const nextView =
        nextSlug === null ||
        ["tokens", "audit", "services", "preferences"].includes(routeState.view)
          ? "overview"
          : routeState.view;
      const nextPath = buildCockpitPath(nextSlug, nextView);
      if (window.location.pathname !== nextPath) {
        window.history.pushState({}, "", nextPath);
      }
      setRouteState({ view: nextView, projectRef: nextSlug, redirectTo: null });
    },
    [routeState.view],
  );

  const onSelectStage = useCallback(
    (stage: StripStage | null) => {
      setStageFilter(stage);
      if (stage) setView("pipeline"); // the board is where the stage filter lives
    },
    [setView],
  );

  const retryProjects = useCallback(async () => {
    const seq = ++projectsSeqRef.current;
    setConnectionsError(null);
    setConnectionsLoaded(false);
    setResolved(null);
    try {
      const settings = await fetchSettings();
      if (seq !== projectsSeqRef.current) return;
      setTenant(settings.tenant);
      const [data, serverProjects] = await Promise.all([
        fetchGitConnections(settings.tenant),
        fetchProjects(settings.tenant),
      ]);
      if (seq !== projectsSeqRef.current) return;
      setConnections(data);
      setProjects(serverProjects.map(toCockpitProject));
      setConnectionsLoaded(true);
    } catch (err) {
      if (seq !== projectsSeqRef.current) return;
      setConnectionsError(err instanceof Error ? err.message : String(err));
      setConnectionsLoaded(true);
    }
  }, []);

  const load = useCallback(async () => {
    try {
      const fresh = await fetchWorkItems();
      setAllItems(fresh);
      ingest(fresh);
      setError(null);
      setTick((t) => t + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    fetchProgress()
      .then((ps) => {
        setProgress(Object.fromEntries(ps.map((p) => [p.correlation_key, p])));
      })
      .catch(() => undefined);
    fetchCards()
      .then((list) => {
        setCards(list);
      })
      .catch(() => undefined);
    void retryProjects();
  }, [ingest, retryProjects]);

  useEffect(() => {
    ensureNotifyPermission();
    fetchHealth()
      .then((health) => setBackend({ kind: "ok", health }))
      .catch((e: unknown) =>
        setBackend({ kind: "error", message: e instanceof Error ? e.message : String(e) }),
      );
    void load();
  }, [load]);

  // Read through a ref so the feed effect does not depend on `load`. Putting
  // `load` in that effect's deps would tear the socket down and reopen it on
  // every change of identity -- reconnect churn caused by the very code meant
  // to make reconnects safe.
  const loadRef = useRef(load);
  loadRef.current = load;
  // Held in a ref so the "first open" state survives re-renders; rebuilding
  // the handler each render would make every open look like the first, and
  // the refetch would never run.
  const refetchOnReopen = useRef(onReopen(() => void loadRef.current()));

  useEffect(() => {
    const ws = openFeed(
      (msg) => {
        if (msg.type === "snapshot") {
          setAllItems(msg.items);
          ingest(msg.items);
        } else if (msg.type === "progress")
          setProgress((prev) => ({ ...prev, [msg.item.correlation_key]: msg.item }));
        else {
          setAllItems((prev) => [
            msg.item,
            ...prev.filter((w) => w.correlation_key !== msg.item.correlation_key),
          ]);
          ingest([msg.item]);
        }
      },
      () => {
        setLive(true);
        // A reconnect must refetch, not just turn the pill green. This feed
        // has no replay: every frame broadcast while the socket was down is
        // gone for good, so a cockpit that only sets live=true keeps
        // rendering pre-drop state underneath a green "live" pill -- stale,
        // and indistinguishable from current. That is the stale-cockpit bug,
        // and it is structural rather than a rendering slip.
        //
        // Refetch rather than replay from a server-side buffer: every frame
        // this feed carries (workitem/progress/snapshot) is authoritative
        // state that can simply be fetched again, and load() already replaces
        // items wholesale. A replay buffer would need sequence numbers, a
        // ring and an expiry policy to reconstruct what one load() returns
        // exactly -- and a buffer can drift from the database, where a
        // refetch cannot. A feed carrying streaming output that CANNOT be
        // refetched would need the buffer; this one does not.
        //
        // Skipped on the first open because the mount effect above already
        // loads; without that the cockpit would fetch twice on every start.
        refetchOnReopen.current();
      },
      () => setLive(false),
    );
    return () => ws.close();
  }, [ingest]);

  const onRefresh = useCallback(async () => {
    setBusy(true);
    try {
      await apiRefresh();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [load]);

  return {
    backend,
    items,
    view: routeState.view,
    setView,
    projectRef: routeState.projectRef,
    activeProject,
    activeProjectSlug,
    projects,
    breadcrumbProjects,
    connectionsLoaded: projectsReady,
    connectionsError: effectiveConnectionsError,
    defaultRepositoryId: connections?.default_repository_id,
    setProjectSlug,
    busy,
    live,
    error,
    tick,
    progress,
    toasts:
      routeState.projectRef === null
        ? toasts
        : toasts.filter((toast) => visibleKeys.has(toast.ev.key)),
    copilotOpen,
    setCopilotOpen,
    stageFilter,
    setStageFilter,
    pins: routeState.projectRef === null ? pins : pins.filter((pin) => visibleKeys.has(pin.key)),
    alarm: routeState.projectRef === null || (alarmKey && visibleKeys.has(alarmKey)) ? alarm : null,
    tickerOpen,
    dismissToast,
    unpin,
    toggleTicker,
    onSelectStage,
    onRefresh,
    retryProjects,
  };
}
