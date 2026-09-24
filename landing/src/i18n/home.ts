import type { Locale } from "./index";

/**
 * The home page's copy. German follows the app's own wording (app_de.arb):
 * "du", Beitrag, Kanal, Token, weiterleiten / verwerfen, bewerten,
 * Warteschlange. The diagram labels are drawn inside a fixed SVG viewBox, so
 * a translation of those has to stay about as short as the English.
 */
const en = {
  title: "Peerkola — a feed carried by people, not an algorithm",
  description:
    "Peerkola is a social feed with no ranking model. Posts travel hand to hand: each reader decides whether one carries on or stops. Posting is paid for with attention you gave first.",
  ogDescription:
    "No ranking model. A post is handed to a few readers; it travels as far as people keep passing it on.",
  appDescription:
    "A social feed with no ranking model: each post is handed to a few readers and travels as far as people keep passing it on.",

  pill: "Public beta",
  heading: "A feed carried by people, not an algorithm.",
  lede:
    "Peerkola has no ranking model. A new post is handed to a handful of readers in its channel, and it travels exactly as far as people keep passing it on. Everything that reaches you is there because someone chose to carry it.",
  openPeerkola: "Open Peerkola",
  seeHow: "See how it works",
  ctaNote: "Runs in the browser · nothing to install · free while in beta.",
  heroArt: "A post being handed from one reader to the next along a chain of four people.",
  heroCaption: "One post, four readers, no algorithm in between.",

  facts: [
    ["No ranking model", "Nothing is scored, boosted, or optimised for time spent."],
    ["Forward or drop", "Every post waits on your call, then leaves your queue for good."],
    ["Posting is earned", "Reviewing earns tokens, publishing spends them. No way to buy in."],
  ],

  howEyebrow: "How it works",
  howHeading: "Five ideas, each one a consequence of the last.",
  howIntro:
    "A post travels by hand, so no ranking model is involved, so the decision is yours — which is worth something, and is therefore priced. What comes out of that is a feed that is allowed to end.",

  ch1Title: "A post travels hand to hand",
  ch1Body:
    "Nothing here is broadcast to everyone at once. A new post is handed to a few readers in its channel. If they pass it on, it reaches the next few — for as long as people keep carrying it.",
  ch1Art: "A post moving along a chain of four readers, lighting each one as it arrives.",

  ch2Title: "No algorithm picks for you",
  ch2Body:
    "A conventional feed ranks everything and serves whatever holds your attention longest. Peerkola has no ranking model at all. What reaches you got here because people, one hop at a time, decided it was worth passing on — and it stops where they stop.",
  ch2ArtBroadcast: "One source broadcasting to all six readers at once.",
  ch2ArtRelay: "The same six readers, reached one hop at a time, and only four of them.",
  ch2LegendConventional: "Conventional feed",

  ch3Title: "You are the next hop",
  ch3Body:
    "Every post in your feed is waiting on your call. Forward it and it travels on to readers who haven’t seen it. Drop it and its journey ends with you. Either way it leaves your queue for good — there is no scrolling past.",
  ch3Art: "A post in your hands, either forwarded on to three more readers, or dropped.",
  ch3Forward: "Forward — it travels on",
  ch3Drop: "Drop — it ends here",

  ch4Title: "Attention is the currency",
  ch4Body:
    "Reviewing other people’s posts earns you tokens. Publishing spends them. So everyone asking for attention has given some first, and there is no shortcut to buy — not with money, not by posting more.",
  ch4Art: "Reviewing posts earns tokens into a balance; publishing a post spends them again.",
  ch4LabelReview: "review",
  ch4LabelBalance: "balance",
  ch4LabelPublish: "publish",
  ch4Earned: "earned by reviewing",
  ch4Spent: "spent by posting",

  ch5Title: "A feed that is allowed to end",
  ch5Body:
    "Your feed is a short queue, not an endless scroll. Sometimes it runs dry — that just means every post out there has already found its readers. It fills again by itself; there is nothing to pull or refresh.",
  ch5Art: "A short queue of posts emptying one at a time, then filling again on its own.",
  ch5Empty: "all caught up — it refills itself",

  betaEyebrow: "Where things stand",
  betaHeading: "Peerkola is in beta",
  betaPoints: [
    "You’re using an early version of the app while it’s still being built.",
    "Bugs and rough edges are expected. If something breaks or feels off, that’s the beta, not you.",
    "Features can change, move, or disappear between versions as things get reworked.",
    "Data may occasionally be reset while the platform is under active development — don’t treat it as permanent yet.",
  ],

  closingEyebrow: "Ready when you are",
  closingHeading: "Carry a few posts. See what reaches you.",
  closingBody:
    "Pick a channel, review what people hand you, and publish once you’ve earned it. Peerkola runs in the browser — there is nothing to install.",
  emailSupport: "Email support",

  contactHeading: "Get in touch",
  contactBody:
    "Questions, bug reports, press, or anything about your account — one address, read by the people building Peerkola.",
  inAppHeading: "Reporting from inside the app",
  inAppBody:
    "The app has a feedback form on the profile screen and on the sign-in screen. The second one works signed out, so “I can’t log in” is still reportable. You can attach a screenshot or a screen recording, and send it anonymously.",
};

// Typed as `en`, so a missing or misnamed key is a type error, not a blank.
export const home: Record<Locale, typeof en> = {
  en,
  de: {
    title: "Peerkola — ein Feed, getragen von Menschen statt Algorithmus",
    description:
      "Peerkola ist ein sozialer Feed ohne Ranking-Algorithmus. Beiträge werden von Hand zu Hand weitergereicht: Jede Person entscheidet, ob ein Beitrag weiterreist oder endet. Posten bezahlst du mit Aufmerksamkeit, die du vorher geschenkt hast.",
    ogDescription:
      "Kein Ranking-Algorithmus. Ein Beitrag geht an wenige Leute – und reist so weit, wie Menschen ihn weitergeben.",
    appDescription:
      "Ein sozialer Feed ohne Ranking-Algorithmus: Jeder Beitrag geht an wenige Leute und reist so weit, wie Menschen ihn weitergeben.",

    pill: "Öffentliche Beta",
    heading: "Ein Feed, getragen von Menschen – nicht von einem Algorithmus.",
    lede:
      "Peerkola hat keinen Ranking-Algorithmus. Ein neuer Beitrag geht an eine Handvoll Leute in seinem Kanal und reist genau so weit, wie Menschen ihn weitergeben. Alles, was bei dir ankommt, ist da, weil jemand entschieden hat, es weiterzutragen.",
    openPeerkola: "Peerkola öffnen",
    seeHow: "So funktioniert’s",
    ctaNote: "Läuft im Browser · keine Installation · kostenlos während der Beta.",
    heroArt: "Ein Beitrag wird entlang einer Kette von vier Personen von einer zur nächsten weitergereicht.",
    heroCaption: "Ein Beitrag, vier Leute, kein Algorithmus dazwischen.",

    facts: [
      ["Kein Ranking", "Nichts wird bewertet, gepusht oder auf Verweildauer optimiert."],
      ["Weiterleiten oder verwerfen", "Jeder Beitrag wartet auf deine Entscheidung und verlässt danach deine Warteschlange."],
      ["Posten verdienst du dir", "Bewerten bringt Token, Veröffentlichen kostet sie. Kaufen kann man sie nicht."],
    ],

    howEyebrow: "So funktioniert’s",
    howHeading: "Fünf Ideen, jede folgt aus der vorigen.",
    howIntro:
      "Ein Beitrag wird von Hand zu Hand gereicht, also braucht es keinen Ranking-Algorithmus, also liegt die Entscheidung bei dir – und die ist etwas wert und hat darum einen Preis. Heraus kommt ein Feed, der auch mal zu Ende sein darf.",

    ch1Title: "Ein Beitrag reist von Hand zu Hand",
    ch1Body:
      "Hier wird nichts an alle auf einmal ausgespielt. Ein neuer Beitrag geht an wenige Leute in seinem Kanal. Geben sie ihn weiter, erreicht er die nächsten – so lange, wie Menschen ihn weitertragen.",
    ch1Art: "Ein Beitrag wandert entlang einer Kette von vier Personen und lässt jede aufleuchten, sobald er ankommt.",

    ch2Title: "Kein Algorithmus wählt für dich aus",
    ch2Body:
      "Ein herkömmlicher Feed sortiert alles und zeigt dir, was deine Aufmerksamkeit am längsten festhält. Peerkola hat überhaupt keinen Ranking-Algorithmus. Was bei dir ankommt, ist hier, weil Menschen Schritt für Schritt entschieden haben, dass es sich lohnt, es weiterzugeben – und es endet dort, wo sie aufhören.",
    ch2ArtBroadcast: "Eine Quelle sendet an alle sechs Personen gleichzeitig.",
    ch2ArtRelay: "Dieselben sechs Personen, Schritt für Schritt erreicht – und nur vier davon.",
    ch2LegendConventional: "Herkömmlicher Feed",

    ch3Title: "Du bist die nächste Station",
    ch3Body:
      "Jeder Beitrag in deinem Feed wartet auf deine Entscheidung. Leitest du ihn weiter, reist er zu Leuten, die ihn noch nicht gesehen haben. Verwirfst du ihn, endet seine Reise bei dir. So oder so verlässt er deine Warteschlange endgültig – einfach vorbeiscrollen gibt es nicht.",
    ch3Art: "Ein Beitrag in deinen Händen: entweder an drei weitere Personen weitergeleitet oder verworfen.",
    ch3Forward: "Weiterleiten – er reist weiter",
    ch3Drop: "Verwerfen – hier endet er",

    ch4Title: "Aufmerksamkeit ist die Währung",
    ch4Body:
      "Wenn du Beiträge anderer bewertest, verdienst du Token. Veröffentlichen kostet sie. Wer um Aufmerksamkeit bittet, hat also vorher selbst welche geschenkt – und es gibt keine Abkürzung: weder mit Geld noch mit mehr Posts.",
    ch4Art: "Beiträge zu bewerten bringt Token aufs Guthaben; einen Beitrag zu veröffentlichen gibt sie wieder aus.",
    ch4LabelReview: "bewerten",
    ch4LabelBalance: "Guthaben",
    ch4LabelPublish: "posten",
    ch4Earned: "verdient durchs Bewerten",
    ch4Spent: "ausgegeben fürs Posten",

    ch5Title: "Ein Feed, der enden darf",
    ch5Body:
      "Dein Feed ist eine kurze Warteschlange, kein endloser Scroll. Manchmal ist er leer – das heißt nur, dass jeder Beitrag da draußen sein Publikum schon gefunden hat. Er füllt sich von selbst wieder; du musst nichts ziehen oder aktualisieren.",
    ch5Art: "Eine kurze Warteschlange von Beiträgen, die sich nach und nach leert und dann von selbst wieder füllt.",
    ch5Empty: "alles erledigt – füllt sich neu",

    betaEyebrow: "Aktueller Stand",
    betaHeading: "Peerkola ist in der Beta",
    betaPoints: [
      "Du nutzt eine frühe Version der App, während sie noch entsteht.",
      "Fehler und Ecken und Kanten sind zu erwarten. Wenn etwas kaputtgeht oder sich komisch anfühlt, liegt das an der Beta, nicht an dir.",
      "Funktionen können sich zwischen Versionen ändern, wandern oder verschwinden, während wir umbauen.",
      "Daten können gelegentlich zurückgesetzt werden, solange die Plattform aktiv entwickelt wird – betrachte sie noch nicht als dauerhaft.",
    ],

    closingEyebrow: "Bereit, wenn du es bist",
    closingHeading: "Trag ein paar Beiträge weiter. Schau, was bei dir ankommt.",
    closingBody:
      "Wähle einen Kanal, bewerte, was dir gereicht wird, und veröffentliche, sobald du es dir verdient hast. Peerkola läuft im Browser – es gibt nichts zu installieren.",
    emailSupport: "Support anschreiben",

    contactHeading: "Kontakt",
    contactBody:
      "Fragen, Fehlerberichte, Presse oder alles rund um dein Konto – eine Adresse, gelesen von den Leuten, die Peerkola bauen.",
    inAppHeading: "Feedback direkt aus der App",
    inAppBody:
      "Die App hat ein Feedback-Formular im Profil und auf dem Anmeldebildschirm. Das zweite funktioniert auch abgemeldet – „Ich kann mich nicht anmelden“ lässt sich also trotzdem melden. Du kannst einen Screenshot oder eine Bildschirmaufnahme anhängen und das Feedback anonym senden.",
  },
};
