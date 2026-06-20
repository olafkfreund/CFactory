// ESLint 9 flat config for the CFactory cockpit frontend (#112).
//
// The strict bar is typescript-eslint's `strictTypeChecked` (type-aware lint)
// plus the React Hooks rules — the same shape the backend's coding standards aim
// for. Because this is being adopted onto an existing legacy frontend, the gate
// is tuned to be GREEN at adoption WITHOUT weakening the bar for new code:
//
//   - Rules that the legacy tree violates pervasively (and that are stylistic /
//     low-risk to defer) are downgraded to "warn", and CI runs with
//     `--max-warnings <baseline>` so net-new warnings still fail the build.
//   - Genuinely important correctness rules stay at "error".
//
// TODO(#112): ratchet the warn-listed rules below back up to "error" as the
// legacy call-sites are cleaned up, then drop --max-warnings to 0. Each is
// annotated with why it's deferred.

import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    // Build output, deps and the config file itself are out of scope.
    ignores: ["dist", "node_modules", "eslint.config.js", "vite.config.ts"],
  },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.strictTypeChecked],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
      parserOptions: {
        project: ["./tsconfig.json"],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],

      // --- Deferred to "warn" for the legacy tree (TODO #112: → "error") ------
      // The cockpit leans on template literals over backend-shaped values
      // (statuses, ids, counts) that are typed `unknown`/`number`/`null`; making
      // this an error would touch dozens of render call-sites at once.
      "@typescript-eslint/restrict-template-expressions": "warn",
      // Legacy `catch {}` / void-returning handlers and a few intentional
      // fire-and-forget effect bodies. Low-risk; clean up incrementally.
      "@typescript-eslint/no-confusing-void-expression": "warn",
      "@typescript-eslint/no-misused-promises": "warn",
      // `??`/`||` and optional-chain tidy-ups across the legacy components.
      "@typescript-eslint/prefer-nullish-coalescing": "warn",
      "@typescript-eslint/no-unnecessary-condition": "warn",
      "@typescript-eslint/no-unnecessary-type-parameters": "warn",
      "@typescript-eslint/no-dynamic-delete": "warn",
      // Legacy uncaught effect/handler promises (fire-and-forget fetch in
      // useEffect) and a handful of `foo!` non-null assertions on values the
      // author knows are present. Behaviour-preserving to leave as-is for now;
      // each wants a `void`/guard when the call-site is next touched.
      "@typescript-eslint/no-floating-promises": "warn",
      "@typescript-eslint/no-non-null-assertion": "warn",
      // Auto-fixable, safe cleanups still present in the legacy tree.
      "@typescript-eslint/no-unnecessary-type-assertion": "warn",
      "@typescript-eslint/no-unnecessary-boolean-literal-compare": "warn",
    },
  },
  // The test file uses vitest globals and intentionally feeds malformed payloads.
  {
    files: ["**/*.test.{ts,tsx}"],
    rules: {
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
    },
  },
);
