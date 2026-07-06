// Portal switcher (#149) — the four Factory portals as one product, in the
// topbar. The current portal (Cockpit) is a static active chip; the others are
// out-links carrying their per-service accent dot. First step of the unified
// shell; SSO handoff so switching doesn't re-login is tracked follow-up.
import { PORTALS } from "./dashboard";

// `needsCount` (#148/#149): fleet count of work items blocked on a human, shown
// as a badge on the Cockpit chip so the shared top bar carries the same "N need
// you" nudge as the sibling portals (which fetch it from /api/needs-you/count).
export default function PortalSwitcher({ needsCount = 0 }: { needsCount?: number }) {
  return (
    <nav className="portal-switch" aria-label="Factory portals">
      {PORTALS.map((p) => {
        const badge = p.key === "cockpit" && needsCount > 0 ? needsCount : 0;
        const inner = (
          <>
            <span className="portal-dot" style={{ background: p.accent }} />
            <span className="portal-label">{p.label}</span>
            {badge > 0 && (
              <span className="nav-badge" aria-label={`${String(badge)} need you`}>
                {badge}
              </span>
            )}
          </>
        );
        return p.current ? (
          <span key={p.key} className="portal-opt on" aria-current="page" title={p.svc}>
            {inner}
          </span>
        ) : (
          <a
            key={p.key}
            className="portal-opt"
            href={p.url}
            title={`Open the ${p.svc} portal`}
            rel="noopener"
          >
            {inner}
          </a>
        );
      })}
    </nav>
  );
}
