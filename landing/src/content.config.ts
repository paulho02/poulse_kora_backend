import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";
import { z } from "astro/zod";

/**
 * Plain text pages - imprint, privacy policy, later info pages - written as
 * Markdown in `src/content/pages/`. The file name is the URL: `imprint.md` is
 * `/imprint/`, `de/impressum.md` is `/de/impressum/`, and the `de/` folder is
 * what makes a page German.
 */
const pages = defineCollection({
  loader: glob({ pattern: "**/*.md", base: "./src/content/pages" }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    /** Not built at all: no page, no sitemap entry, no footer link. */
    draft: z.boolean().default(false),
    /** Built and reachable, but asks search engines to leave it out. */
    noindex: z.boolean().default(false),
    /** Listed among the legal links in the footer. */
    legal: z.boolean().default(false),
    updated: z.coerce.date().optional(),
    /**
     * Shared by a page and its translations (`imprint.md` and
     * `de/impressum.md` both say `imprint`), so each links to the others via
     * hreflang and the language switch lands on the counterpart, not the home page.
     */
    translationKey: z.string().optional(),
  }),
});

export const collections = { pages };
