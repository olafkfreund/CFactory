// The cockpit's data + notification plumbing, extracted from App.tsx (#115):
// work-item state, live progress, the WebSocket feed, the toast/pin/alarm
// notification machine, and the refresh/load actions. App becomes a thin
// composition root that consumes this hook. Pure extraction — the logic, effect
// ordering and dependency arrays are unchanged.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchHealth,
  fetchProgress,
  fetchWorkItems,
  openFeed,
  refresh as apiRefresh,
  type LiveProgress,
  type WorkItem,
} from "./api";
import { onReopen } from "./onReopen";
import { diffEvents } from "./taskEvents";
import { ensureNotifyPermission, osNotify, type TaskEvent } from "./notify";
import { stageState, type TaskState } from "./taskState";
import type { StripAlarm, StripStage } from "./PipelineStrip";
import type { TickerPin } from "./EventTicker";
import { TICKER_PREF, type Backend, type View } from "./dashboard";

export interface ToastEntry {
  id: number;
  ev: TaskEvent;
}

export interface Dashboard {
  backend: Backend;
  items: WorkItem[];
  view: View;
  setView: (v: View) => void;
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
}

export function useDashboard(): Dashboard {
  const [backend, setBackend] = useState<Backend>({ kind: "loading" });
  const [items, setItems] = useState<WorkItem[]>([]);
  const [view, setView] = useState<View>("overview");
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

  const ingest = useCallback((incoming: WorkItem[]) => {
    const events = diffEvents(statesRef.current, incoming, seededRef.current);
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

  const onSelectStage = useCallback((stage: StripStage | null) => {
    setStageFilter(stage);
    if (stage) setView("pipeline"); // the board is where the stage filter lives
  }, []);

  const load = useCallback(async () => {
    try {
      const fresh = await fetchWorkItems();
      setItems(fresh);
      ingest(fresh);
      setError(null);
      setTick((t) => t + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    fetchProgress()
      .then((ps) => setProgress(Object.fromEntries(ps.map((p) => [p.correlation_key, p]))))
      .catch(() => undefined);
  }, [ingest]);

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
          setItems(msg.items);
          ingest(msg.items);
        } else if (msg.type === "progress")
          setProgress((prev) => ({ ...prev, [msg.item.correlation_key]: msg.item }));
        else {
          setItems((prev) => [
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
    view,
    setView,
    busy,
    live,
    error,
    tick,
    progress,
    toasts,
    copilotOpen,
    setCopilotOpen,
    stageFilter,
    setStageFilter,
    pins,
    alarm,
    tickerOpen,
    dismissToast,
    unpin,
    toggleTicker,
    onSelectStage,
    onRefresh,
  };
}
