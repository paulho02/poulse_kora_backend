import sitemap from "@astrojs/sitemap";
import { defineConfig } from "astro/config";

import { DEFAULT_LOCALE, LOCALES } from "./src/i18n/index.ts";
import { SITE_URL } from "./src/site.ts";

// Every page is prerendered to plain HTML at build time; `dist/` is the whole
// site and any static host can serve it (see README.md for how Railway does).
export default defineConfig({
  site: SITE_URL,
  // `/imprint/` is built as `imprint/index.html`, and the canonical URL, the
  // sitemap and Caddy's directory redirect all agree on the trailing slash.
  trailingSlash: "always",
  build: { format: "directory" },
  // English lives at the root, other languages under their prefix (`/de/...`).
  // The list itself is src/i18n/index.ts.
  i18n: {
    defaultLocale: DEFAULT_LOCALE,
    locales: [...LOCALES],
    routing: { prefixDefaultLocale: false },
  },
  // Set by docker-compose.override.yml: a bind mount doesn't forward file
  // events, so the dev server in Docker has to poll to notice an edit.
  vite: {
    server: { watch: { usePolling: process.env.ASTRO_WATCH_POLLING === "true" } },
  },
  integrations: [
    sitemap({
      // Adds xhtml:link alternates between pages whose paths differ only by
      // the locale prefix (`/` and `/de/`). Translated slugs (`/imprint/` vs
      // `/de/impressum/`) are paired by the pages' own hreflang links instead.
      i18n: {
        defaultLocale: DEFAULT_LOCALE,
        locales: Object.fromEntries(LOCALES.map((l) => [l, l])),
      },
      // A noindex page must not also be offered to crawlers.
      filter: (page) => !/(^|\/)404\/?$/.test(new URL(page).pathname.replace(/\/$/, "")),
    }),
  ],
});
