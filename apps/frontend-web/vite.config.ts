import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Cockpit dev server on 3110, proxying API calls to the backend on 3111.
// Override the backend target by editing this file or running behind a gateway.
const BACKEND = "http://localhost:3111";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3110,
    proxy: {
      "/health": BACKEND,
      // ws: true so the /api/ws cockpit feed proxies through too.
      "/api": { target: BACKEND, ws: true },
    },
  },
});
