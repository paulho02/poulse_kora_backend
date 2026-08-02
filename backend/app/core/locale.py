"""Locale resolution from the client's `Accept-Language` header.

No per-user persistence: the mobile app decides its own active locale locally
(device locale, or a manual override in Settings) and sends it on every
request instead. Header-per-request, not a `User.locale` column, is what lets
the pre-login banner (GET /banner, no user yet) resolve a locale at all.
"""

import re

from app.core.config import settings

_LANG_TAG = re.compile(r"^[a-zA-Z]{2,3}")


def parse_accept_language(header: str | None) -> str:
    """The first supported language in an `Accept-Language` header's preference
    order, or `settings.DEFAULT_LOCALE` if none match.

    Deliberately simple: splits on commas, ignores `;q=` weights (the client is
    expected to send its actual active locale first, not a weighted list),
    and matches on the primary subtag only (`de-DE` -> `de`), the granularity
    `SUPPORTED_LOCALES` is defined at.
    """
    if not header:
        return settings.DEFAULT_LOCALE
    for part in header.split(","):
        tag = part.split(";", 1)[0].strip()
        match = _LANG_TAG.match(tag)
        if not match:
            continue
        primary = match.group(0).lower()
        if primary in settings.SUPPORTED_LOCALES:
            return primary
    return settings.DEFAULT_LOCALE
