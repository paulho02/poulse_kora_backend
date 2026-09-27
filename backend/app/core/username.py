"""Deriving a username for accounts created without one.

Registration requires a username (`UserCreate.username`), but Google sign-in has no
such field - the client never asks, because there is nothing to ask *before* the
Google account is known. So a Google signup gets one derived from its Google profile
here, and confirms/edits it during onboarding (the app's username step).

What a username may be at all is app/core/username_policy.py; everything produced
here obeys it.
"""

import re
import secrets
import unicodedata
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.user import User

#: Long enough to stay recognizable, short enough to leave room for a suffix.
MAX_LENGTH = 20
#: Used when the seed slugifies to nothing at all (e.g. a name in a non-Latin script).
FALLBACK = "user"
#: Sequential suffixes first, so the common case reads naturally (paul, paul2, paul3);
#: after this many collisions we stop scanning and go random.
_SEQUENTIAL_ATTEMPTS = 20


def slugify_username(seed: str) -> str:
    """Reduce `seed` to lowercase alphanumerics, truncated to MAX_LENGTH.

    Accents are folded rather than dropped (NFKD, then the combining marks go), so
    `José` seeds `jose`, not `jos`. Unlike a name the user types, this one is ours
    to choose, and onboarding shows it for editing. Too short to be a valid name
    (a two-letter display name) falls back like an empty one.
    """
    ascii_seed = (
        unicodedata.normalize("NFKD", seed).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]", "", ascii_seed.lower())[:MAX_LENGTH]
    return slug if len(slug) >= settings.USERNAME_MIN_LENGTH else FALLBACK


async def generate_unique_username(session: AsyncSession, *, seed: str) -> str:
    """Return a username derived from `seed` that no user currently holds.

    Best-effort, not a reservation: `User.username` is unique, so two concurrent
    signups deriving the same seed can still collide on INSERT. The caller is
    expected to treat that as retryable rather than rely on this being atomic - see
    app/api/google_auth.py.
    """
    base = slugify_username(seed)

    for attempt in range(_SEQUENTIAL_ATTEMPTS):
        candidate = base if attempt == 0 else f"{base[: MAX_LENGTH - 2]}{attempt + 1}"
        if not await is_username_taken(session, candidate):
            return candidate

    # A popular base name. Stop probing one at a time and jump somewhere sparse.
    while True:
        candidate = f"{base[: MAX_LENGTH - 6]}{secrets.randbelow(1_000_000):06d}"
        if not await is_username_taken(session, candidate):
            return candidate


async def is_username_taken(
    session: AsyncSession, username: str, *, exclude_user_id: uuid.UUID | None = None
) -> bool:
    """Whether another account already holds `username`.

    Matched exactly, the way the unique constraint does. Exact is enough: every
    stored name is normalized (app/core/username_policy.py, enforced by a CHECK
    constraint), so there is no second spelling of the same name to look for.
    `exclude_user_id` is for updates, so re-sending your own name is not a clash.

    Like `generate_unique_username`, this is a probe and not a reservation: the
    INSERT/UPDATE can still lose a race, which is why every caller also treats the
    IntegrityError as the same refusal (see app/deps/users.py).
    """
    query = select(User.id).where(User.username == username).limit(1)
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)
    result = await session.execute(query)
    return result.first() is not None
