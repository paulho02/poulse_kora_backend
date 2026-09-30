import type { Locale } from "./index";

/** Strings in the shared layout, the docs chrome and the not-found page. */
const en = {
  skipToContent: "Skip to content",
  navLabel: "Main",
  navHow: "How it works",
  navVision: "Vision",
  navDocs: "Docs",
  navContact: "Contact",
  themeToggle: "Switch between light and dark",
  openApp: "Open the app",
  legalPending: "Imprint and privacy policy follow with the public launch.",
  lastUpdated: "Last updated",
  notFoundTitle: "Page not found",
  notFoundHeading: "This page doesn’t exist.",
  notFoundBody: "It may have moved, or the link has a typo. The home page explains what Peerkola is.",
  notFoundCta: "Go to the home page",

  docsTitle: "Documentation",
  docsDescription:
    "Everything you need to know to use Peerkola: the feed, forwarding and dropping, tokens and prices, trust, channels and languages, your account and your data.",
  docsIntro:
    "Everything you need to use Peerkola, from your first post to deleting your account. Start at the top if you’re new; otherwise jump to whatever you’re looking for.",
  docsNavLabel: "Documentation",
  docsOnThisPage: "On this page",
  docsPrev: "Previous",
  docsNext: "Next",
  docsMenu: "All topics",
  docsQuestion: "Something missing or unclear?",
  docsQuestionBody: "Write to us, or use the feedback form in the app. Every question tells us what to explain better.",
};

// Typed as `en`, so a key missing here is a type error rather than a blank.
export const ui: Record<Locale, typeof en> = {
  en,
  de: {
    skipToContent: "Zum Inhalt springen",
    navLabel: "Hauptmenü",
    navHow: "So funktioniert’s",
    navVision: "Vision",
    navDocs: "Doku",
    navContact: "Kontakt",
    themeToggle: "Zwischen hell und dunkel wechseln",
    openApp: "App öffnen",
    legalPending: "Impressum und Datenschutzerklärung folgen zum öffentlichen Start.",
    lastUpdated: "Stand",
    notFoundTitle: "Seite nicht gefunden",
    notFoundHeading: "Diese Seite gibt es nicht.",
    notFoundBody: "Vielleicht ist sie umgezogen, oder im Link steckt ein Tippfehler. Auf der Startseite steht, was Peerkola ist.",
    notFoundCta: "Zur Startseite",

    docsTitle: "Dokumentation",
    docsDescription:
      "Alles, was du für Peerkola wissen musst: Feed, Weiterleiten und Verwerfen, Token und Preise, Vertrauen, Kanäle und Sprachen, dein Konto und deine Daten.",
    docsIntro:
      "Alles, was du für Peerkola brauchst, vom ersten Beitrag bis zum Löschen deines Kontos. Neu hier? Dann fang oben an. Sonst spring direkt zu dem, was du suchst.",
    docsNavLabel: "Dokumentation",
    docsOnThisPage: "Auf dieser Seite",
    docsPrev: "Zurück",
    docsNext: "Weiter",
    docsMenu: "Alle Themen",
    docsQuestion: "Fehlt etwas, oder ist etwas unklar?",
    docsQuestionBody: "Schreib uns, oder nutz das Feedback-Formular in der App. Jede Frage zeigt uns, was wir besser erklären müssen.",
  },
};
