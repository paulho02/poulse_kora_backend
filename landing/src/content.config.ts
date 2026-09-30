import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";
import { z } from "astro/zod";

import { DOC_SECTIONS } from "./i18n/docs";

/**
 * Plain text pages - imprint, privacy policy, vision, later info pages -
 * written as Markdown in `src/content/pages/`. The file name is the URL:
 * `imprint.md` is `/imprint/`, `de/impressum.md` is `/de/impressum/`, and the
 * `de/` folder is what makes a page German.
 */
const pages = defineCollection({
  loader: glob({ pattern: "**/*.md", base: "./src/content/pages" }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    /** Small label above the title (the vision page uses it). */
    eyebrow: z.string().optional(),
    /** An introductory paragraph set larger, between the title and the text. */
    lede: z.string().optional(),
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

/**
 * The user documentation: `src/content/docs/<locale>/<slug>.md` is served at
 * `/docs/<slug>/` (English) or `/de/docs/<slug>/` (German). A page and its
 * translation share the slug, which is what pairs them - see src/lib/docs.ts.
 */
const docs = defineCollection({
  loader: glob({ pattern: "**/*.md", base: "./src/content/docs" }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    /** Which sidebar group the page belongs to; the groups are src/i18n/docs.ts. */
    section: z.enum(DOC_SECTIONS),
    /** Position within its section, ascending. */
    order: z.number(),
    draft: z.boolean().default(false),
  }),
});

export const collections = { pages, docs };
