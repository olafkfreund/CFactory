import { describe, expect, it, vi } from "vitest";
import { onReopen } from "./onReopen";

describe("onReopen", () => {
  it("does not fire on the first open", () => {
    const fn = vi.fn();
    onReopen(fn)();
    expect(fn).not.toHaveBeenCalled();
  });

  it("fires on every open after the first", () => {
    const fn = vi.fn();
    const handler = onReopen(fn);
    handler(); // initial connect
    handler(); // reconnect 1
    handler(); // reconnect 2
    // Three opens, two of them reconnects. A guard that latched after the
    // first reconnect would leave the cockpit stale on every drop after it.
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("keeps separate state per feed", () => {
    const a = vi.fn();
    const b = vi.fn();
    const ha = onReopen(a);
    const hb = onReopen(b);
    ha();
    ha();
    hb();
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).not.toHaveBeenCalled();
  });
});
