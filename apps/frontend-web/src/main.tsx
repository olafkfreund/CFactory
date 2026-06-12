import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import TokenPage from "./TokenPage";
import { startVersionWatch } from "./versionWatch";
import "./index.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("root element not found");
}

// Auto-reload when a newer bundle is deployed, so stale tabs self-heal.
startVersionWatch();

// /settings/token (#73) is a standalone page with its own URL — the manual
// fallback for the factory-vscode token hand-off. Everything else is the cockpit.
const isTokenPage = window.location.pathname.replace(/\/+$/, "") === "/settings/token";

createRoot(root).render(
  <StrictMode>{isTokenPage ? <TokenPage /> : <App />}</StrictMode>,
);
