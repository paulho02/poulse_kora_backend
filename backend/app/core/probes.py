"""Test posts: minting them, and recording how they were answered.

A probe is an ordinary `Post` row (`Post.is_probe`) whose text asks, in its own words,
to be forwarded or dropped. It is the only input to Reviewer Trust that a script cannot
imitate - pacing and forward rates can both be faked, but answering a differently worded
instruction cannot be, short of reading it.

Seven decisions here are load-bearing:

- **A real post, delivered through the real queue.** `place_post` puts it in the
  reader's queue exactly as fan-out would, so it renders, opens, and is reviewed through
  the paths a real post uses. Nothing about how it *arrives* can give it away, and there
  is no second code path to keep in step when the feed changes.

- **Minted per reader, never shared.** A pool of reusable probe posts would run into the
  seen-set exclusion (a reader may only ever be handed a given post once), so a heavy
  reader would exhaust it and stop being measured. One row per probe costs an insert
  at `TRUST_PROBE_RATE` of reviews, which is nothing next to what it buys.

- **Minted on the review path, not in the worker.** The worker is deliberately
  pure-Redis and never opens a Postgres session (see CLAUDE.md); this needs one.
  `review_post` is already a write transaction on the same reader, and rolling the dice
  *after a review* is what makes `TRUST_PROBE_RATE` mean literally what it says: the
  chance that the next post to arrive is a test.

- **No fan-out operation is minted for a probe, ever.** Not on creation, and not when a
  reader forwards one. So probes never enter `keys.OPS_OUTSTANDING`, never count as
  congestion, and never move the admission price - a measurement that made posting more
  expensive would be charging authors for the privilege of being measured.

- **The author is a real, named account.** If every probe were anonymous, "anonymous"
  would become the tell, and every genuinely anonymous post would inherit the suspicion.
  The account is created on demand, cannot sign in (inactive, random password), and its
  address is under a domain RFC 2606 reserves, so it can never be registered and mail to
  it goes nowhere.

- **Randomness comes from `SystemRandom`.** Whether the next post is a probe is the one
  thing an adversary would most like to predict; a seeded PRNG makes that a question
  worth asking, and this makes it unaskable for the cost of nothing.

- **Probe rows are never cleaned up by age, and must not be.** This looks like an
  omission and is not. A probe sits in its reader's queue until they review it, however
  long that takes, and answering a two-month-old one counts in full - because the score
  reads `ProbeResponse.created`, the moment it was *answered*, not `Post.created`, the
  moment it was minted. So the two obvious deletion keys are both wrong: pruning by mint
  age would throw away responses that are live evidence inside the trust window, and
  any deletion at all can hit a post still sitting in somebody's queue - which Postgres
  has no way to detect, since the queue is a Redis list of ids - leaving that reader a
  ghost card. A probe row is a post like any other; the table keeps those forever too.
"""

import secrets
import uuid

from fastapi_users.password import PasswordHelper
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import trust_service
from app.core.config import settings
from app.core.logger import get_logger
from app.core.probe_templates import ProbeVariant, probe_languages, variants_for
from app.feed import keys as feed_keys
from app.feed import service
from app.models.channel_subscription import ChannelSubscription
from app.models.post import Post
from app.models.post_block import PostBlock
from app.models.probe_response import ProbeResponse
from app.models.user import User

log = get_logger(__name__)

_random = secrets.SystemRandom()

# How long an unanswered probe blocks the next one. Deliberately a constant rather than
# a setting: it is not a dial anyone should be turning, it is the answer to a single
# question - how long is a probe still plausibly going to be answered? A reader working
# through a queue answers within a session; one that has sat a day is one the reader has
# decided to leave. Blocking on it any longer would let a single ignored probe stop that
# reader's trust from ever being measured again, which is the one failure mode that
# compounds: no probes means no evidence means a score frozen at neutral forever.
PENDING_TTL_SECONDS = 24 * 60 * 60


def _pending_key(user_id: str) -> str:
    """Post id of the probe this reader has out, if any."""
    return f"trust:probe:pending:{user_id}"


def _since_key(user_id: str) -> str:
    """Real reviews since this reader's last probe."""
    return f"trust:probe:since:{user_id}"


def _recent_key(user_id: str) -> str:
    """Variant codes recently shown to this reader (newest first)."""
    return f"trust:probe:recent:{user_id}"


async def _ensure_probe_author(session: AsyncSession) -> User:
    """The account probes are published as, created on first use.

    Inactive and holding a random password it never sees again, so it is an identity
    rather than a login: `authenticate` refuses an inactive user, and nobody holds the
    secret in any case. Verified, so nothing in the registration machinery ever tries
    to mail the reserved address it uses.

    Normally already there: alembic 0005 mints it, so that on a fresh deployment the
    identity exists before any user could register it. The lookup insists on
    `is_active = false` for the same reason - an *active* account at this address is
    somebody's, not ours, and publishing test posts under it would hand that person
    every probe in the deployment. In that case the insert below fails on the unique
    email, `review_post` logs `probe.mint_failed`, and no probe is minted, which is
    the safe failure. The tests' `create_all` schema never runs the migration, so the
    on-demand path is still exercised.
    """
    author = await session.scalar(
        select(User).where(
            User.email == settings.TRUST_PROBE_AUTHOR_EMAIL,
            User.is_active.is_(False),
        )
    )
    if author is not None:
        return author
    author = User(
        id=uuid.uuid4(),
        email=settings.TRUST_PROBE_AUTHOR_EMAIL,
        username=settings.TRUST_PROBE_AUTHOR_USERNAME,
        hashed_password=PasswordHelper().hash(secrets.token_urlsafe(32)),
        is_active=False,
        is_verified=True,
        is_superuser=False,
    )
    session.add(author)
    await session.commit()
    await session.refresh(author)
    log.info("probe.author_created", user_id=str(author.id))
    return author


def _pick_variant(language: str, recent: list[str]) -> ProbeVariant | None:
    """A variant in `language` the reader has not seen lately.

    Falls back to the full set once every variant is recent, rather than refusing to
    probe: a reader who has worked through the whole catalogue is exactly the engaged
    reader the score most wants evidence about, and a repeat after several hundred
    reviews is not a pattern anyone is learning from.
    """
    available = variants_for(language)
    if not available:
        return None
    unseen = [variant for variant in available if variant.code not in recent]
    return _random.choice(unseen or available)


async def should_mint(redis: Redis, user_id: str) -> bool:
    """Roll for whether this reader's next post should be a test.

    Three gates, cheapest first. A probe already outstanding means the reader has one to
    answer and does not need a second; `TRUST_PROBE_MIN_GAP_REVIEWS` stops the dice from
    landing twice in quick succession, which is both irritating and the fastest way to
    teach someone what a probe looks like; and only then does the rate apply.

    The counter is incremented on every call, so it counts *reviews* - which is what
    makes the rate mean "your next post" rather than "your next visit".
    """
    if not settings.TRUST_ENABLED or settings.TRUST_PROBE_RATE <= 0:
        return False
    if await redis.exists(_pending_key(user_id)):
        return False
    since = await redis.incr(_since_key(user_id))
    if since < settings.TRUST_PROBE_MIN_GAP_REVIEWS:
        return False
    return _random.random() < settings.TRUST_PROBE_RATE


async def mint_probe(
    session: AsyncSession, redis: Redis, user: User
) -> Post | None:
    """Create a test post for `user` and place it in their queue. None if not possible.

    Returns None rather than raising for every ordinary reason a probe cannot be made -
    the reader subscribes to nothing, reads a language with no probe copy, or has a full
    queue. A probe is a measurement of opportunity; skipping one costs a little
    confidence and nothing else, so none of these is worth failing a review over.
    """
    languages = [
        language
        for language in probe_languages()
        if language in set(user.content_languages or [])
    ]
    if not languages:
        return None
    language = _random.choice(languages)

    channel_id = await session.scalar(
        select(ChannelSubscription.channel_id)
        .where(ChannelSubscription.user_id == user.id)
        .order_by(func.random())
        .limit(1)
    )
    if channel_id is None:
        return None

    # Placing into a full queue would push past FEED_QUEUE_MAX_SLOTS (the `place`
    # script drops the reader from free_queue but does not refuse), and the overflow
    # entry would sit beyond the window `render_queue_ids` reads - invisible, but
    # occupying the slot. Checking first is cheaper than the alternative of teaching
    # the script a second refusal mode.
    if await redis.llen(feed_keys.queue(str(user.id))) >= settings.FEED_QUEUE_MAX_SLOTS:
        return None

    recent = [
        code.decode() if isinstance(code, bytes) else code
        for code in await redis.lrange(
            _recent_key(str(user.id)), 0, settings.TRUST_PROBE_RECENT_MEMORY - 1
        )
    ]
    variant = _pick_variant(language, recent)
    if variant is None:
        return None

    author = await _ensure_probe_author(session)
    post = Post(
        channel_id=channel_id,
        author_id=author.id,
        is_anonymous=False,
        is_probe=True,
        language=language,
    )
    session.add(post)
    await session.flush()
    session.add(
        PostBlock(
            post_id=post.id,
            position=0,
            block_type="text",
            text=variant.texts[language],
        )
    )
    await session.commit()

    placed = await service.place_post(redis, str(user.id), post.id)
    if placed == service.PLACE_REFUSED:
        # Cannot happen for a post that was created a moment ago (nobody can be in its
        # seen set), but `place_post` is the choke point every delivery goes through and
        # its contract says a refusal is never a delivery. Treat it as one rather than
        # leaving a probe row nobody will ever answer.
        log.warning("probe.place_refused", post_id=post.id)
        return None

    await redis.set(_pending_key(str(user.id)), post.id, ex=PENDING_TTL_SECONDS)
    await redis.set(_since_key(str(user.id)), 0)
    await redis.lpush(_recent_key(str(user.id)), variant.code)
    await redis.ltrim(
        _recent_key(str(user.id)), 0, settings.TRUST_PROBE_RECENT_MEMORY - 1
    )
    await redis.expire(
        _recent_key(str(user.id)), settings.TRUST_WINDOW_DAYS * 24 * 60 * 60
    )

    # INFO: one line per probe, which is TRUST_PROBE_RATE of reviews - rare enough to
    # keep at this level, and the only record that a given reader was measured at all.
    log.info(
        "probe.minted",
        post_id=post.id,
        channel_id=channel_id,
        language=language,
        variant=variant.code,
        expected_kind=variant.expected_kind,
    )
    return post


async def variant_for_post(session: AsyncSession, post: Post) -> ProbeVariant | None:
    """Recover which variant a probe post was written from, by matching its text.

    Matching text rather than storing the variant code on `Post` keeps a column off a
    table every feed query reads, for the sake of a lookup that happens once per probe.
    The cost is that editing a variant's wording orphans probes already in flight -
    which is why `answer` treats an unrecognised probe as unscorable and drops the
    measurement, rather than guessing at what it used to ask.

    The text is fetched with its own query rather than off `post.blocks`, because the
    review path deliberately loads the post with a bare `session.get` and a lazy
    relationship access is a hard error under async. One indexed read, on the one review
    in twenty that is a probe.
    """
    text = await session.scalar(
        select(PostBlock.text)
        .where(PostBlock.post_id == post.id, PostBlock.block_type == "text")
        .order_by(PostBlock.position)
        .limit(1)
    )
    if text is None:
        return None
    for variant in variants_for(post.language):
        if variant.texts.get(post.language) == text:
            return variant
    return None


async def answer(
    session: AsyncSession, redis: Redis, user: User, post: Post, kind: str
) -> bool | None:
    """Record how `user` answered probe `post`. Returns whether they got it right.

    None means the probe could not be scored - its wording no longer matches any
    variant, so there is no expected verdict to compare against. The reader still earns
    their token and the post still leaves their queue; only the measurement is dropped,
    because a guess about what a since-edited post used to ask would be worse than no
    data.

    Invalidates the cached score, rather than waiting out `TRUST_CACHE_TTL_SECONDS`:
    this is the strongest and rarest input the score has, and a reader who has just
    passed or failed one should see the result while they still remember answering it.
    """
    await redis.delete(_pending_key(str(user.id)))

    variant = await variant_for_post(session, post)
    if variant is None:
        log.warning("probe.unrecognised", post_id=post.id, language=post.language)
        return None

    correct = kind == variant.expected_kind
    session.add(
        ProbeResponse(
            user_id=user.id,
            post_id=post.id,
            variant_code=variant.code,
            expected_kind=variant.expected_kind,
            given_kind=kind,
            correct=correct,
        )
    )
    await session.commit()
    await trust_service.invalidate(redis, str(user.id))

    log.info(
        "probe.answered",
        post_id=post.id,
        variant=variant.code,
        expected_kind=variant.expected_kind,
        given_kind=kind,
        correct=correct,
    )
    return correct


async def purge_user(redis: Redis, user_id: str) -> None:
    """Drop every probe/trust key belonging to a deleted account."""
    await redis.delete(
        _pending_key(user_id),
        _since_key(user_id),
        _recent_key(user_id),
        trust_service.cache_key(user_id),
    )
