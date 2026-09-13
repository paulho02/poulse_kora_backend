"""The language axis of the feed's routing key.

A post is written in exactly one language; a reader accepts a set of them; fan-out
delivers a post only where the two meet (see app/feed/keys.py: audience). This module
is the single place that knows which values are legal and how a bad or stale one
degrades.

Two asymmetries are worth knowing before touching `Settings.CONTENT_LANGUAGES`:

- **Adding** a language is safe and additive. No existing post or reader changes
  meaning; the new language's audience sets simply start empty and fill as readers
  opt in. Run `python -m scripts.dangerous.rebuild_redis` so the sets exist for
  readers who had already subscribed.
- **Removing** one is not the inverse. Posts already written in it keep that value in
  Postgres, and readers keep it in `User.content_languages`. Both are *read* through
  the helpers here, which drop unknown codes rather than raising - so a removed
  language degrades to "this post can no longer be routed to anyone" and "this reader
  accepts one fewer language", never to a 500. A reader left with an empty set after
  the filter falls back to the default (see `sanitize_reading_languages`), because an
  empty set means an empty feed forever and that is never what an operator meant.
"""

from collections.abc import Iterable

from app.core.config import LANGUAGE_UNSPECIFIED, settings

# Re-exported under a shorter name; defined in config.py so this module can import
# `settings` without a cycle. See the constant's own comment there.
UNSPECIFIED = LANGUAGE_UNSPECIFIED


def reading_languages() -> list[str]:
    """Languages a *reader* may accept - the configured set, without UNSPECIFIED.

    UNSPECIFIED is deliberately not offerable here. A post that declares it routes
    through the whole channel, which every subscriber is a member of regardless of
    language, so "accepting" it would be a setting that could not be switched off and
    would change nothing when switched on.
    """
    return list(settings.CONTENT_LANGUAGES)


def post_languages() -> list[str]:
    """Languages a *post* may declare - the configured set plus UNSPECIFIED, last.

    Ordered with UNSPECIFIED at the end because this list drives the client's picker
    and "no language" belongs under the real options, not among them.
    """
    return [*settings.CONTENT_LANGUAGES, UNSPECIFIED]


def is_post_language(value: str) -> bool:
    """Is `value` something a post may declare?"""
    return value in post_languages()


def sanitize_reading_languages(values: Iterable[str]) -> list[str]:
    """Normalize a reader's accepted-language set: lowercased, de-duplicated, ordered
    to match CONTENT_LANGUAGES, and stripped of anything no longer configured.

    Never returns empty. A set that filters down to nothing - every language the
    reader chose has since been removed from the deployment - falls back to
    `default_reading_languages()`, because the alternative is a reader whose fan-out
    audience is nowhere and whose feed is permanently empty with no error to explain
    it. Ordering by CONTENT_LANGUAGES rather than by input makes the stored value a
    canonical form, so two equivalent sets compare equal and a no-op update is
    recognizable as one.
    """
    wanted = {code.strip().lower() for code in values if code and code.strip()}
    cleaned = [code for code in settings.CONTENT_LANGUAGES if code in wanted]
    return cleaned or default_reading_languages()


def default_reading_languages(locale: str | None = None) -> list[str]:
    """What a brand-new account accepts, resolved from the request's locale.

    Just the one language, not every configured one. A reader who is handed posts in
    a language they cannot read is the entire problem this feature exists to fix, and
    defaulting wide would reproduce it for exactly the users who never visit the
    setting. The cost is the opposite failure - a reader of a language with little
    supply seeing a thin feed - which is visible, self-explanatory and one toggle
    away, and which UNSPECIFIED posts partly cover anyway.

    Falls back to DEFAULT_LOCALE, then to the first configured language, so this
    always returns a non-empty list even if DEFAULT_LOCALE is not itself a content
    language.
    """
    for candidate in (locale, settings.DEFAULT_LOCALE):
        if candidate and candidate in settings.CONTENT_LANGUAGES:
            return [candidate]
    return [settings.CONTENT_LANGUAGES[0]]
