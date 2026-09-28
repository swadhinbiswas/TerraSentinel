import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

/**
 * Test setup.
 *
 * The dashboard had no test runner at all, which is why a hand-rolled oklch→hex
 * conversion, a SQL deny-list and a WebGL feature probe were all shipping unverified.
 * `happy-dom` rather than jsdom: the components under test touch `getComputedStyle`,
 * `ResizeObserver` and `document.documentElement.dataset`, and happy-dom is a fraction of
 * the install.
 *
 * The alias mirrors `tsconfig.json`. Without it, importing `@/lib/utils` from a spec
 * resolves to nothing and the failure reads as a missing module rather than a config gap.
 */
export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "happy-dom",
    include: ["src/**/*.test.ts"],
    // The build output directory and the Cloudflare types both sit in the project, and
    // Vitest's default glob would try to walk them.
    exclude: ["node_modules/**", "dist/**", ".wrangler/**"],
  },
});
