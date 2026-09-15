"""The text of a test post, in every language a post can be written in.

A probe is a post that asks, in its own words, to be forwarded or dropped. Answering
it as instructed is the one thing in the whole trust score that cannot be imitated by
a script pacing itself and forwarding a plausible fraction of its queue: it requires
having read the post.

Kept apart from `app/core/probes.py` (which is about minting and scoring them) for the
same reason `email_templates.py` is kept apart from `email.py` - this file is copy, and
copy is what changes most often and needs the least context to change safely.

Four things are load-bearing:

- **Keyed by content language, not by locale.** A probe is a *post*, so its text has to
  exist in the language the post declares (`CONTENT_LANGUAGES`), which is a different
  list from the locales the API answers requests in (`SUPPORTED_LOCALES`). They happen
  to hold the same codes today and are free to diverge tomorrow; conflating them is the
  mistake CLAUDE.md's Localization note warns about. There is no per-key English
  fallback here either, unlike `email_templates._t`: a German post silently written in
  English is a mislabelled post, and mislabelled posts are what the language routing
  economy is trying to correct, not produce.

- **Both verdicts, in roughly equal numbers.** If every probe asked to be dropped, the
  winning strategy would be "drop anything that looks like a test" - pattern-matching,
  which is precisely what this is meant to distinguish from reading. It is also what
  fixes `TRUST_PROBE_CHANCE_RATE` at 0.5: a reader answering blindly converges there,
  and the score maps that to zero credit.

- **The wording varies and the instruction is buried.** A reader who has learned to
  react to the first four words is not reading either, so no two variants open the same
  way, and the ask is never the opening clause. `tests/core/test_probe_templates.py`
  asserts both of those, because both fail silently - a duplicated opener still renders
  perfectly and simply stops measuring anything.

- **They read as ordinary posts, not as system notices.** No shouting, no all-caps, no
  "SYSTEM MESSAGE". A probe that announces itself typographically is one every reader
  learns to spot at a glance, and then it measures attention to formatting rather than
  to text. The visible marker the client renders (see `PostRead.is_probe`) is the honest
  disclosure; the text does not need to be a second one.
"""

from dataclasses import dataclass

from app.core.config import settings

FORWARD = "forward"
DROP = "drop"


@dataclass(frozen=True)
class ProbeVariant:
    """One test post: what it asks for, and how it asks in each language."""

    code: str
    expected_kind: str
    texts: dict[str, str]


# Ten variants, five of each verdict. The count matters less than the ratio and the
# spread of phrasings: at TRUST_PROBE_RATE = 0.05 and a memory of the last six shown
# (TRUST_PROBE_RECENT_MEMORY), a reader would have to review several hundred posts
# before a variant could repeat.
VARIANTS: tuple[ProbeVariant, ...] = (
    ProbeVariant(
        code="quality_check_drop",
        expected_kind=DROP,
        texts={
            "en": (
                "Quick quality check - this post carries no real content. If you have "
                "read this far, please drop it so it goes no further."
            ),
            "de": (
                "Kurze Qualitätsprüfung - dieser Beitrag hat keinen echten "
                "Inhalt. Wenn du bis hierher gelesen hast, verwirf ihn bitte, "
                "damit er nicht weiterläuft."
            ),
        },
    ),
    ProbeVariant(
        code="attention_check_forward",
        expected_kind=FORWARD,
        texts={
            "en": (
                "You are reading a check, not a post. Nothing here is meant for anyone "
                "else - but to confirm you read it, forward this one instead of "
                "dropping it."
            ),
            "de": (
                "Du liest gerade eine Prüfung, keinen Beitrag. Hier steht "
                "nichts, das für andere gedacht wäre - aber um zu bestätigen, dass "
                "du gelesen hast, leite diesen bitte weiter, statt ihn zu verwerfen."
            ),
        },
    ),
    ProbeVariant(
        code="placeholder_drop",
        expected_kind=DROP,
        texts={
            "en": (
                "There is no story in this post. It exists to find out whether posts "
                "are being read before they are judged, and the right answer is to "
                "drop it."
            ),
            "de": (
                "In diesem Beitrag steckt keine Geschichte. Er existiert, um "
                "herauszufinden, ob Beiträge gelesen werden, bevor über sie "
                "entschieden wird - richtig ist hier, ihn zu verwerfen."
            ),
        },
    ),
    ProbeVariant(
        code="handshake_forward",
        expected_kind=FORWARD,
        texts={
            "en": (
                "Every so often the feed slips in a post like this one to see whether "
                "anyone is still reading. Forward it, and it counts as read."
            ),
            "de": (
                "Ab und zu mischt der Feed einen Beitrag wie diesen dazwischen, um zu "
                "sehen, ob noch jemand mitliest. Leite ihn weiter, dann gilt er als "
                "gelesen."
            ),
        },
    ),
    ProbeVariant(
        code="empty_page_drop",
        expected_kind=DROP,
        texts={
            "en": (
                "Imagine this space were an article you had been waiting for. It is "
                "not one - it is a test, and a test belongs nowhere but here. Drop it."
            ),
            "de": (
                "Stell dir vor, hier stünde der Artikel, auf den du gewartet hast. Das "
                "ist er nicht - das ist ein Test, und ein Test gehört nirgendwo hin "
                "außer hierher. Verwirf ihn."
            ),
        },
    ),
    ProbeVariant(
        code="signal_forward",
        expected_kind=FORWARD,
        texts={
            "en": (
                "Nothing in this post is worth passing on, which is exactly why it is "
                "here: pass it on anyway, so it is clear the words were read and not "
                "skipped."
            ),
            "de": (
                "Nichts an diesem Beitrag ist es wert, weitergegeben zu werden - genau "
                "deshalb steht er hier: gib ihn trotzdem weiter, damit klar ist, dass "
                "die Worte gelesen und nicht übersprungen wurden."
            ),
        },
    ),
    ProbeVariant(
        code="no_author_drop",
        expected_kind=DROP,
        texts={
            "en": (
                "Nobody wrote this hoping it would travel. It is a check on how "
                "carefully the feed is being read, and the way to answer it is to let "
                "it stop with you."
            ),
            "de": (
                "Niemand hat das hier geschrieben, in der Hoffnung, dass es "
                "weiterzieht. Es ist eine Prüfung, wie aufmerksam der Feed gelesen "
                "wird - und die Antwort darauf ist, es bei dir enden zu lassen."
            ),
        },
    ),
    ProbeVariant(
        code="relay_forward",
        expected_kind=FORWARD,
        texts={
            "en": (
                "Treat the next sentence as the whole point of this post: send it on. "
                "It is a check, and sending it on is how the check is passed."
            ),
            "de": (
                "Nimm den nächsten Satz als den ganzen Sinn dieses Beitrags: "
                "schick ihn weiter. Es ist eine Prüfung, und weiterschicken ist "
                "die Art, sie zu bestehen."
            ),
        },
    ),
    ProbeVariant(
        code="filler_drop",
        expected_kind=DROP,
        texts={
            "en": (
                "Somewhere in a feed full of things people meant, this one was written "
                "by nobody and means nothing. Reading it is the task; dropping it is "
                "the answer."
            ),
            "de": (
                "Irgendwo in einem Feed voller Dinge, die jemand ernst gemeint hat, "
                "steht dieser hier, den niemand geschrieben hat und der nichts "
                "bedeutet. Ihn zu lesen ist die Aufgabe; ihn zu verwerfen ist die "
                "Antwort."
            ),
        },
    ),
    ProbeVariant(
        code="one_more_forward",
        expected_kind=FORWARD,
        texts={
            "en": (
                "Read to the end and the instruction is simple enough: this is a test "
                "post, and it wants to be forwarded rather than dropped."
            ),
            "de": (
                "Lies bis zum Ende, dann ist die Anweisung einfach genug: das ist ein "
                "Testbeitrag, und er möchte weitergeleitet und nicht verworfen werden."
            ),
        },
    ),
)


def variants_for(language: str) -> list[ProbeVariant]:
    """Every variant available in `language`.

    Filters rather than falls back, so a language added to `CONTENT_LANGUAGES` before
    its probe copy exists simply stops being probed instead of being probed in English.
    A reader who is never probed sits at neutral trust, which is the correct handling of
    "we have no evidence"; a reader probed in a language they do not read would be
    scored on our omission.
    """
    return [variant for variant in VARIANTS if language in variant.texts]


def probe_languages() -> list[str]:
    """Content languages a probe can currently be written in.

    The intersection of what is configured and what is actually translated, in
    `CONTENT_LANGUAGES` order. `probes.mint_probe` picks a reader's probe language from
    this, so an untranslated language is skipped rather than being served English text
    under a German label.
    """
    return [
        language
        for language in settings.CONTENT_LANGUAGES
        if any(language in variant.texts for variant in VARIANTS)
    ]
