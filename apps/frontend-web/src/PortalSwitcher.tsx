// Portal switcher (#149) — the four Factory portals as one product, in the
// topbar. The current portal (Cockpit) is a static active chip; the others are
// out-links carrying their per-service accent dot. First step of the unified
// shell; SSO handoff so switching doesn't re-login is tracked follow-up.
import { PORTALS } from "./dashboard";

export default function PortalSwitcher() {
  return (
    <nav className="portal-switch" aria-label="Factory portals">
      {PORTALS.map((p) =>
        p.current ? (
          <span key={p.key} className="portal-opt on" aria-current="page" title={p.svc}>
            <span className="portal-dot" style={{ background: p.accent }} />
            <span className="portal-label">{p.label}</span>
          </span>
        ) : (
          <a
            key={p.key}
            className="portal-opt"
            href={p.url}
            title={`Open the ${p.svc} portal`}
            rel="noopener"
          >
            <span className="portal-dot" style={{ background: p.accent }} />
            <span className="portal-label">{p.label}</span>
          </a>
        ),
      )}
    </nav>
  );
}
