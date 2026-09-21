import cloudflare from "@astrojs/cloudflare";
import react from "@astrojs/react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "astro/config";

// Static HTML with React islands, deployed to Cloudflare Pages.
//
// The adapter matters: API routes run as Pages Functions inside `workerd`, which
// cannot open a raw TCP connection. Turso is reached over its HTTP pipeline API,
// which is why no proxy service sits between the dashboard and the database.
export default defineConfig({
  site: "https://terrasentinel-dashboard.pages.dev",
  output: "server",
  adapter: cloudflare({ platformProxy: { enabled: true } }),
  integrations: [react()],
  vite: {
    plugins: [tailwindcss()],
    // Secrets are read at request time from the Pages environment, never bundled.
    define: {},
  },
});
