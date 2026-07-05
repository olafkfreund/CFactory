// Error boundary (#146) — the cockpit is an always-on monitoring surface, so a
// single render throw must not blank the whole screen. This catches it and shows
// a recover card (reload / retry) with the message, instead of a white page.
// Kept deliberately small and dependency-free; the sibling portals already ship
// one, this brings CFactory to parity.
import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = { children: ReactNode; label?: string };
type State = { error: Error | null };

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface it in the console for the operator / log capture; no telemetry dep.
    console.error("[cockpit] render error:", error, info.componentStack);
  }

  private reset = () => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="eb" role="alert">
        <div className="eb-card">
          <div className="eb-title">The {this.props.label ?? "cockpit"} hit an error</div>
          <p className="eb-msg">
            A view failed to render. Your data is safe — this is a display error. Try again, or
            reload if it persists.
          </p>
          <pre className="eb-detail">{error.message}</pre>
          <div className="eb-actions">
            <button className="eb-btn eb-btn--primary" onClick={this.reset}>
              Try again
            </button>
            <button
              className="eb-btn"
              onClick={() => {
                window.location.reload();
              }}
            >
              Reload cockpit
            </button>
          </div>
        </div>
      </div>
    );
  }
}
