/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Optional link to the OpenObserve / OTLP backend UI (RFC-0001 v1.3 P2).
  // Build-time configurable; defaults at runtime to the public instance. The
  // cockpit links out to it in a new tab — never an iframe — to keep auth
  // simple. Set to an empty string to hide the link entirely.
  readonly VITE_OBSERVE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
