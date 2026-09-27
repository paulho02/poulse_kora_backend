"""What a username may be: lowercase ASCII letters, digits and `_`.

A username is shown on every non-anonymous post, so it is the one thing on an
account other people read as *who wrote this* - and anything two people can see as
the same name is an impersonation. Exact-match uniqueness alone let `Peerkola`,
`peerkοla` (Greek omicron) and ` peerkola` coexist with the probe author and with
each other. The rule closes that by construction rather than by a confusables
table: with one case and one script there is nothing left to confuse.

Two steps, deliberately separate:

- `normalize_username` is applied to every incoming name (the schemas' validators)
  and is *not* a refusal - `Paul` simply is `paul`. NFKC first, so a fullwidth
  `ｐａｕｌ` from an East Asian keyboard folds to the same name instead of being
  refused.
- `is_valid_username` is the refusal, answered as `username_invalid` by
  `UserManager`. Whatever survives normalization outside `[a-z0-9_]` (accents,
  other scripts, spaces, punctuation) is the user's to change, not ours to strip:
  silently turning `josé` into `jos` would pick a name for them.

No imports on purpose: config validates the probe author's name against this, and
config is imported by nearly everything else.
"""

import re
import unicodedata

#: Stored form. Also a CHECK constraint on `users.username` (alembic 0007), so a
#: script that bypasses `UserManager` fails loudly instead of storing a lookalike.
USERNAME_PATTERN = re.compile(r"[a-z0-9_]+")


def normalize_username(raw: str) -> str:
    """The form a name is compared and stored in. Never refuses."""
    return unicodedata.normalize("NFKC", raw).strip().lower()


def is_valid_username(name: str, *, min_length: int, max_length: int) -> bool:
    """Whether an already-normalized name obeys the rule."""
    return (
        min_length <= len(name) <= max_length
        and USERNAME_PATTERN.fullmatch(name) is not None
    )
