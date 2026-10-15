// "Do this on every open except the first."
//
// The cockpit feed reconnects on its own, and a reconnect has to refetch:
// the feed has no replay, so every frame broadcast while the socket was down
// is gone for good. A client that only turns its "live" pill green keeps
// rendering pre-drop state, which is stale and indistinguishable from
// current -- the stale-cockpit bug, structural rather than a rendering slip.
//
// The first open is different: whatever mounted the feed has normally just
// loaded, so refetching there would double every start-up.
//
// This lives in its own module, rather than as a boolean ref inline in the
// hook, because the ordering IS the logic and inline it cannot be tested --
// the hook needs a React renderer this package does not carry.
export function onReopen(fn: () => void): () => void {
  let opened = false;
  return () => {
    // Set AFTER the call, not before, or the first open would refetch too.
    if (opened) fn();
    opened = true;
  };
}
