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
    "No ranking model. A post is handed to a few readers and travels as far as people keep passing it on.",
  appDescription:
    "A social feed with no ranking model: each post is handed to a few readers and travels as far as people keep passing it on.",

  pill: "Public beta",
  heading: "A feed carried by people, not an algorithm.",
  lede:
    "Peerkola has no ranking model. A new post goes to a handful of readers in its channel, and it travels exactly as far as people keep passing it on. Everything that reaches you is there because someone chose to carry it.",
  openPeerkola: "Open Peerkola",
  seeHow: "See how it works",
  ctaNote: "Runs in the browser · nothing to install · free while in beta.",
  heroArt: "A post being handed from one reader to the next along a chain of four people.",
  heroCaption: "One post, four readers, no algorithm in between.",

  facts: [
    ["No ranking model", "Nothing is scored, boosted, or optimised for time spent."],
    ["Forward or drop", "Every post waits for your decision, then leaves your queue."],
    ["Posting is earned", "Reviewing earns tokens, publishing spends them. You can’t buy them."],
  ],

  howEyebrow: "How it works",
  howHeading: "Five ideas that build on each other.",
  howIntro:
    "Posts are passed on by hand, so there is nothing to rank. That leaves the decision with you, and because your attention is worth something, posting costs some. The result is a feed that is allowed to end.",

  ch1Title: "A post travels hand to hand",
  ch1Body:
    "Nothing here is broadcast to everyone at once. A new post goes to a few readers in its channel. If they pass it on, it reaches the next few, and so on for as long as people keep carrying it.",
  ch1Art: "A post moving along a chain of four readers, lighting each one as it arrives.",

  ch2Title: "No algorithm picks for you",
  ch2Body:
    "A conventional feed ranks everything and serves whatever holds your attention longest. Peerkola has no ranking model at all. What reaches you got here because people, one hop at a time, decided it was worth passing on. Where they stop, it stops.",
  ch2ArtBroadcast: "One source broadcasting to all six readers at once.",
  ch2ArtRelay: "The same six readers, reached one hop at a time, and only four of them.",
  ch2LegendConventional: "Conventional feed",

  ch3Title: "You are the next hop",
  ch3Body:
    "Every post in your feed is waiting for your decision. Forward it and it goes on to readers who haven’t seen it. Drop it and its journey ends with you. Either way it leaves your queue for good. There is no scrolling past.",
  ch3Art: "A post in your hands, either forwarded on to three more readers, or dropped.",
  ch3Forward: "Forward — it travels on",
  ch3Drop: "Drop — it ends here",

  ch4Title: "Attention is the currency",
  ch4Body:
    "Reviewing other people’s posts earns you tokens. Publishing spends them. So everyone asking for attention has given some first, and there is no shortcut: not with money, and not by posting more.",
  ch4Art: "Reviewing posts earns tokens into a balance; publishing a post spends them again.",
  ch4LabelReview: "review",
  ch4LabelBalance: "balance",
  ch4LabelPublish: "publish",
  ch4Earned: "earned by reviewing",
  ch4Spent: "spent by posting",

  ch5Title: "A feed that is allowed to end",
  ch5Body:
    "Your feed is a short queue, not an endless scroll. Sometimes it runs dry. That only means every post out there has already found its readers. New ones arrive on their own; there is nothing to pull or refresh.",
  ch5Art: "A short queue of posts emptying one at a time, then filling again on its own.",
  ch5Empty: "all caught up — it refills itself",

  moreEyebrow: "Read on",
  visionHeading: "Why we’re building this",
  visionBody:
    "What’s wrong with the feeds we have, what Peerkola does differently, and what it deliberately won’t do.",
  visionCta: "Read the vision",
  docsHeading: "How everything works",
  docsBody:
    "Tokens, prices, trust, channels, languages, your account and your data. The documentation covers it all, step by step.",
  docsCta: "Open the documentation",

  betaEyebrow: "Where things stand",
  betaHeading: "Peerkola is in beta",
  betaPoints: [
    "You’re using an early version of the app while it’s still being built.",
    "Expect bugs and rough edges. If something breaks or feels off, that’s the beta, not you.",
    "Features can change, move, or disappear between versions.",
    "Data may occasionally be reset while the platform is under active development, so don’t treat it as permanent yet.",
  ],

  closingEyebrow: "Try it",
  closingHeading: "Carry a few posts. See what reaches you.",
  closingBody:
    "Pick a channel, review what people hand you, and publish once you’ve earned it. Peerkola runs in the browser; there is nothing to install.",
  emailSupport: "Email support",

  contactHeading: "Get in touch",
  contactBody:
    "Questions, bug reports, press, or anything about your account: one address, read by the people building Peerkola.",
  inAppHeading: "Reporting from inside the app",
  inAppBody:
    "The app has a feedback form on the profile screen and on the sign-in screen. The second one works signed out, so “I can’t log in” is still reportable. You can attach a screenshot or a screen recording, and send it anonymously.",
};

// Typed as `en`, so a missing or misnamed key is a type error, not a blank.
export const home: Record<Locale, typeof en> = {
  en,
  de: {
    title: "Peerkola – ein Feed, den Menschen weiterreichen, kein Algorithmus",
    description:
      "Peerkola ist ein sozialer Feed ohne Ranking-Algorithmus. Beiträge gehen von Hand zu Hand: Wer einen bekommt, entscheidet, ob er weitergeht. Wer posten will, hat vorher selbst gelesen.",
    ogDescription:
      "Kein Ranking-Algorithmus. Ein Beitrag geht an ein paar Leute und kommt so weit, wie sie ihn weitergeben.",
    appDescription:
      "Ein sozialer Feed ohne Ranking-Algorithmus: Jeder Beitrag geht an ein paar Leute und kommt so weit, wie sie ihn weitergeben.",

    pill: "Öffentliche Beta",
    heading: "Was du hier liest, hat dir ein Mensch weitergereicht.",
    lede:
      "Peerkola sortiert nichts für dich. Ein neuer Beitrag landet bei ein paar Leuten in seinem Kanal und kommt genau so weit, wie sie ihn weitergeben. Alles in deinem Feed ist dort, weil sich jemand dafür entschieden hat.",
    openPeerkola: "Peerkola öffnen",
    seeHow: "Wie es funktioniert",
    ctaNote: "Läuft im Browser, ohne Installation. Kostenlos, solange die Beta läuft.",
    heroArt: "Ein Beitrag wird entlang einer Kette von vier Personen weitergereicht.",
    heroCaption: "Ein Beitrag, vier Leute, kein Algorithmus dazwischen.",

    facts: [
      ["Kein Ranking", "Nichts wird gewichtet, gepusht oder auf Verweildauer optimiert."],
      ["Weiterleiten oder verwerfen", "Jeder Beitrag wartet auf deine Entscheidung. Danach ist er aus deinem Feed raus."],
      ["Posten muss man sich verdienen", "Bewerten bringt Token, Veröffentlichen kostet welche. Kaufen kann man sie nicht."],
    ],

    howEyebrow: "So funktioniert’s",
    howHeading: "Fünf Regeln, die aufeinander aufbauen.",
    howIntro:
      "Beiträge werden von Hand weitergegeben, deshalb gibt es nichts zu sortieren. Die Entscheidung liegt bei dir, und weil deine Aufmerksamkeit etwas wert ist, kostet Posten etwas. Am Ende steht ein Feed, der auch mal leer sein darf.",

    ch1Title: "Ein Beitrag geht von Hand zu Hand",
    ch1Body:
      "Nichts wird an alle gleichzeitig ausgespielt. Ein neuer Beitrag geht zuerst an ein paar Leute in seinem Kanal. Geben sie ihn weiter, kommt er bei den nächsten an, und so weiter, solange ihn jemand weiterträgt.",
    ch1Art: "Ein Beitrag wandert eine Kette von vier Personen entlang; jede leuchtet auf, sobald er bei ihr ankommt.",

    ch2Title: "Kein Algorithmus sucht für dich aus",
    ch2Body:
      "Ein üblicher Feed sortiert alles und zeigt dir, was dich am längsten festhält. Peerkola sortiert gar nicht. Was bei dir ankommt, haben Menschen einer nach dem anderen für weitergebenswert gehalten. Wo sie aufhören, hört auch der Beitrag auf.",
    ch2ArtBroadcast: "Eine Quelle schickt an alle sechs Personen gleichzeitig.",
    ch2ArtRelay: "Dieselben sechs Personen, nacheinander erreicht, und nur vier von ihnen.",
    ch2LegendConventional: "Üblicher Feed",

    ch3Title: "Als Nächstes bist du dran",
    ch3Body:
      "Jeder Beitrag in deinem Feed wartet auf deine Entscheidung. Leitest du ihn weiter, geht er an Leute, die ihn noch nicht kennen. Verwirfst du ihn, ist bei dir Schluss. In beiden Fällen verschwindet er aus deinem Feed. Einfach drüberscrollen geht nicht.",
    ch3Art: "Ein Beitrag bei dir: Entweder geht er an drei weitere Personen, oder er wird verworfen.",
    ch3Forward: "Weiterleiten – er geht weiter",
    ch3Drop: "Verwerfen – hier ist Schluss",

    ch4Title: "Aufmerksamkeit ist die Währung",
    ch4Body:
      "Wer Beiträge anderer bewertet, bekommt Token. Wer veröffentlicht, gibt sie aus. Jeder, der hier Aufmerksamkeit will, hat also vorher selbst welche gegeben. Eine Abkürzung gibt es nicht, weder mit Geld noch durch Masse.",
    ch4Art: "Bewerten bringt Token aufs Guthaben, Veröffentlichen gibt sie wieder aus.",
    ch4LabelReview: "bewerten",
    ch4LabelBalance: "Guthaben",
    ch4LabelPublish: "posten",
    ch4Earned: "durchs Bewerten verdient",
    ch4Spent: "fürs Posten ausgegeben",

    ch5Title: "Ein Feed mit Ende",
    ch5Body:
      "Dein Feed ist eine kurze Warteschlange, kein endloser Strom. Manchmal ist sie leer. Das heißt nur, dass gerade jeder Beitrag seine Leser gefunden hat. Neue kommen von allein, du musst nichts aktualisieren.",
    ch5Art: "Eine kurze Warteschlange, die sich Beitrag für Beitrag leert und dann von selbst wieder füllt.",
    ch5Empty: "alles gelesen – Nachschub kommt",

    moreEyebrow: "Weiterlesen",
    visionHeading: "Warum wir das bauen",
    visionBody:
      "Was an den heutigen Feeds nicht stimmt, was Peerkola anders macht und was es bewusst nicht tun wird.",
    visionCta: "Zur Vision",
    docsHeading: "Wie alles funktioniert",
    docsBody:
      "Token, Preise, Vertrauen, Kanäle, Sprachen, dein Konto und deine Daten. In der Dokumentation steht alles der Reihe nach.",
    docsCta: "Zur Dokumentation",

    betaEyebrow: "Stand der Dinge",
    betaHeading: "Peerkola ist in der Beta",
    betaPoints: [
      "Die App ist noch im Bau. Du nutzt eine frühe Version.",
      "Es wird Fehler geben und Stellen, die noch haken. Wenn etwas nicht funktioniert, liegt das an der Beta und nicht an dir.",
      "Funktionen können sich von Version zu Version ändern, woanders landen oder wegfallen.",
      "Solange wir aktiv entwickeln, können Daten zurückgesetzt werden. Verlass dich noch nicht darauf, dass alles bleibt.",
    ],

    closingEyebrow: "Probier’s aus",
    closingHeading: "Gib ein paar Beiträge weiter und schau, was bei dir ankommt.",
    closingBody:
      "Such dir einen Kanal aus, bewerte, was dir weitergereicht wird, und veröffentliche, sobald du genug Token hast. Peerkola läuft im Browser, installieren musst du nichts.",
    emailSupport: "E-Mail an den Support",

    contactHeading: "Kontakt",
    contactBody:
      "Fragen, Fehler, Presseanfragen oder etwas zu deinem Konto: Schreib an diese Adresse. Die Mails lesen die Leute, die Peerkola bauen.",
    inAppHeading: "Feedback aus der App",
    inAppBody:
      "In der App gibt es ein Feedback-Formular im Profil und auf dem Anmeldebildschirm. Das zweite geht auch ohne Anmeldung, damit du auch „Ich komme nicht rein“ melden kannst. Screenshots oder Bildschirmaufnahmen kannst du anhängen, und auf Wunsch schickst du alles anonym.",
  },
};
