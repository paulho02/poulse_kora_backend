import type { Locale } from "./index";

/** Strings in the shared layout and the not-found page. */
const en = {
  skipToContent: "Skip to content",
  navLabel: "Main",
  navHow: "How it works",
  navBeta: "Beta",
  navContact: "Contact",
  themeToggle: "Switch between light and dark",
  openApp: "Open the app",
  legalPending: "Imprint and privacy policy follow with the public launch.",
  lastUpdated: "Last updated",
  notFoundTitle: "Page not found",
  notFoundHeading: "This page doesn’t exist.",
  notFoundBody: "It may have moved, or the link was mistyped. The home page explains what Peerkola is.",
  notFoundCta: "Go to the home page",
};

// Typed as `en`, so a key missing here is a type error rather than a blank.
export const ui: Record<Locale, typeof en> = {
  en,
  de: {
    skipToContent: "Zum Inhalt springen",
    navLabel: "Hauptmenü",
    navHow: "So funktioniert’s",
    navBeta: "Beta",
    navContact: "Kontakt",
    themeToggle: "Zwischen hell und dunkel wechseln",
    openApp: "App öffnen",
    legalPending: "Impressum und Datenschutzerklärung folgen zum öffentlichen Start.",
    lastUpdated: "Stand",
    notFoundTitle: "Seite nicht gefunden",
    notFoundHeading: "Diese Seite gibt es nicht.",
    notFoundBody: "Vielleicht wurde sie verschoben, oder der Link ist vertippt. Auf der Startseite erfährst du, was Peerkola ist.",
    notFoundCta: "Zur Startseite",
  },
};
