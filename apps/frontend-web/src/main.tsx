import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { startVersionWatch } from "./versionWatch";
import "./index.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("root element not found");
}

// Auto-reload when a newer bundle is deployed, so stale tabs self-heal.
startVersionWatch();

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
