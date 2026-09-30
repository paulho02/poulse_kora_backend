import type { Locale } from "./index";

/**
 * The documentation's sidebar groups, in the order they are shown. A doc page
 * names its group in its frontmatter (`section: reading`); adding a group means
 * adding it here and giving it a label in every language below.
 */
export const DOC_SECTIONS = ["start", "reading", "posting", "trust", "account", "help"] as const;
export type DocSection = (typeof DOC_SECTIONS)[number];

export const docSectionLabels: Record<Locale, Record<DocSection, string>> = {
  en: {
    start: "Getting started",
    reading: "Reading",
    posting: "Posting",
    trust: "Trust",
    account: "Account and privacy",
    help: "Help",
  },
  de: {
    start: "Erste Schritte",
    reading: "Lesen",
    posting: "Posten",
    trust: "Vertrauen",
    account: "Konto und Datenschutz",
    help: "Hilfe",
  },
};
