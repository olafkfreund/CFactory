import { useEffect, useRef, useState } from "react";
import { useReducedMotion } from "framer-motion";

/** Animate a number toward `value` with an eased count-up. Honors reduced motion. */
export function useCountUp(value: number, ms = 700): number {
  const reduced = useReducedMotion();
  const [n, setN] = useState(value);
  const from = useRef(value);

  useEffect(() => {
    if (reduced || from.current === value) {
      setN(value);
      from.current = value;
      return;
    }
    const start = performance.now();
    const a = from.current;
    let raf = 0;
    const tick = (t: number) => {
      const p = Math.min(1, (t - start) / ms);
      const eased = 1 - Math.pow(1 - p, 3);
      setN(Math.round(a + (value - a) * eased));
      if (p < 1) raf = requestAnimationFrame(tick);
      else from.current = value;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, ms, reduced]);

  return n;
}

/** A monotonically-updating `Date.now()` that re-renders on an interval, but
 *  only while `active` — so per-node live timers tick once a second when work
 *  is in flight and go quiet (no timers, no churn) the moment it settles. */
export function useNow(active: boolean, everyMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(id);
  }, [active, everyMs]);
  return now;
}
