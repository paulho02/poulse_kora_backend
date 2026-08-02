"""The single app-wide info banner (superuser-set, publicly readable).

Stores one message per locale so `GET /banner` can resolve to whatever the
requester's `Accept-Language` says (see app.deps.locale). No TTL: unlike the
feed's price snapshot (app/feed/service.py), this must persist indefinitely
until an admin explicitly replaces or clears it — there is no "stale" state to
expire out of.
"""

import json
import time
import uuid
from typing import TypedDict

from redis.asyncio import Redis

from app.core.config import settings

KEY = "banner:current"


class Banner(TypedDict):
    id: str
    messages: dict[str, str]  # locale -> text; "en" always present
    set_at: float


def _resolve_message(messages: dict[str, str], locale: str) -> str:
    return (
        messages.get(locale)
        or messages.get(settings.DEFAULT_LOCALE)
        or next(iter(messages.values()))
    )


def _normalize(raw: dict) -> Banner:
    """Back-compat for a banner written before locales existed: that shape
    stored a single `message: str` instead of `messages: dict[str, str]`.
    Reading one of those treats it as an English-only banner rather than
    erroring or silently dropping it."""
    messages = raw.get("messages")
    if messages is None:
        messages = {"en": raw["message"]}
    return {"id": raw["id"], "messages": messages, "set_at": raw["set_at"]}


async def get_banner(redis: Redis, locale: str) -> dict | None:
    """The current banner resolved to `locale` (falling back to
    `settings.DEFAULT_LOCALE`, then to whatever locale actually got stored),
    or None if nothing has been set. Shape: `{id, message, set_at}` — a single
    resolved string, matching `BannerRead`."""
    raw = await redis.get(KEY)
    if raw is None:
        return None
    banner = _normalize(json.loads(raw))
    return {
        "id": banner["id"],
        "message": _resolve_message(banner["messages"], locale),
        "set_at": banner["set_at"],
    }


async def set_banner(redis: Redis, messages: dict[str, str]) -> Banner:
    """Upsert the banner with a fresh id, storing every locale's text.

    The id is random rather than derived from the message text, so re-pushing
    identical text is still a new event: a client that dismissed an earlier
    message "forever" is unaffected by this one. `messages` must include "en"
    (enforced by `BannerSet`); other supported locales are optional.
    """
    banner: Banner = {
        "id": uuid.uuid4().hex,
        "messages": messages,
        "set_at": time.time(),
    }
    await redis.set(KEY, json.dumps(banner))
    return banner


async def clear_banner(redis: Redis) -> None:
    await redis.delete(KEY)
