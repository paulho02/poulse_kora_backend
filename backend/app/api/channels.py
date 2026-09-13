from fastapi import APIRouter
from sqlalchemy import select

from app.core import languages
from app.core.errors import api_error
from app.core.logger import get_logger
from app.deps.db import CurrentAsyncSession
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentVerifiedUser
from app.feed import service
from app.models.channel import Channel
from app.models.channel_subscription import ChannelSubscription
from app.schemas.channel import ChannelRead

log = get_logger(__name__)

router = APIRouter(prefix="/channels")


async def _subscribed_channel_ids(session: CurrentAsyncSession, user_id) -> set[int]:
    result = await session.execute(
        select(ChannelSubscription.channel_id).filter(
            ChannelSubscription.user_id == user_id
        )
    )
    return set(result.scalars().all())


async def _channel_reads(
    session: CurrentAsyncSession,
    redis,
    user,
    channels: list[Channel],
) -> list[ChannelRead]:
    """Serialize channels with their price ranges, pricing every route of every one.

    Pricing all `channels x (languages + 1)` routes rather than only the ones this user
    could post to is what keeps the deployment-wide range honest: `keys.PRICE_RANGE` is
    observed from computations like this one, and a request that priced only its own
    languages would leave the global range reporting a subset. The cost is unchanged in
    round trips - `service.route_prices` batches the whole set (see its docstring) - and
    it is why no background enumeration of routes is needed anywhere.
    """
    subscribed_ids = await _subscribed_channel_ids(session, user.id)
    routes = [
        (channel.id, language)
        for channel in channels
        for language in languages.post_languages()
    ]
    prices = await service.route_prices(redis, routes)
    return [
        ChannelRead(
            id=channel.id,
            name=channel.name,
            color=channel.color,
            description=channel.description,
            is_subscribed=channel.id in subscribed_ids,
            post_price_min=min(
                prices[(channel.id, lang)] for lang in languages.post_languages()
            ),
            post_price_max=max(
                prices[(channel.id, lang)] for lang in languages.post_languages()
            ),
        )
        for channel in channels
    ]


@router.get("", response_model=list[ChannelRead])
async def list_channels(
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    redis: CurrentRedis,
    q: str | None = None,
):
    """List channels, each with the range its routes currently cost to post in.

    Prices come back for the whole page in one `service.route_prices` call (a few
    round trips regardless of page size or language count) rather than per route — this
    endpoint is what backs the price display a user leaves switched on while they review
    and wait to afford a post, so it is read far more often than it changes. That
    frequency is also what makes it the natural place to observe the deployment-wide
    price range from; see `_channel_reads`.
    """
    query = select(Channel).order_by(Channel.name)
    if q:
        query = query.filter(
            Channel.name.ilike(f"%{q}%") | Channel.description.ilike(f"%{q}%")
        )
    channels = list((await session.execute(query)).scalars().all())
    return await _channel_reads(session, redis, user, channels)


@router.post("/{channel_id}/subscribe", response_model=ChannelRead)
async def subscribe_channel(
    channel_id: int,
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    redis: CurrentRedis,
):
    channel = await session.get(Channel, channel_id)
    if not channel:
        raise api_error(404, "channel_not_found")

    existing = await session.scalar(
        select(ChannelSubscription).filter(
            ChannelSubscription.user_id == user.id,
            ChannelSubscription.channel_id == channel_id,
        )
    )
    if not existing:
        session.add(ChannelSubscription(user_id=user.id, channel_id=channel_id))
        await session.commit()

    # Mirror into Redis: join every audience set this reader belongs in for the channel
    # (the channel set plus one per language they accept) and make them reachable by
    # fan-out (in free_queue if their queue has room).
    #
    # Deliberately no history backfill. A new subscriber's queue fills from the worker
    # alone: posts published from now on, plus any parked in ops:retry because the
    # channel had no free recipient — which is exactly the backlog case. Pulling
    # already-distributed history here would be a second delivery path racing those
    # retries, which is how the same post used to land in the queue twice.
    await service.sync_subscribe(
        redis, str(user.id), channel_id, user.content_languages
    )
    # Subscriptions are what fan-out actually targets, so their rate is the one
    # number that explains a channel's delivery behaviour changing.
    log.info("channel.subscribed", channel_id=channel_id, already=bool(existing))

    return (await _channel_reads(session, redis, user, [channel]))[0]


@router.post("/{channel_id}/unsubscribe", response_model=ChannelRead)
async def unsubscribe_channel(
    channel_id: int,
    session: CurrentAsyncSession,
    user: CurrentVerifiedUser,
    redis: CurrentRedis,
):
    channel = await session.get(Channel, channel_id)
    if not channel:
        raise api_error(404, "channel_not_found")

    existing = await session.scalar(
        select(ChannelSubscription).filter(
            ChannelSubscription.user_id == user.id,
            ChannelSubscription.channel_id == channel_id,
        )
    )
    if existing:
        await session.delete(existing)
        await session.commit()

    # Mirror into Redis: remove the user from every one of the channel's audience sets
    # so they no longer receive fan-out from it in any language. Already-queued posts
    # are left in place.
    await service.sync_unsubscribe(redis, str(user.id), channel_id)
    log.info(
        "channel.unsubscribed", channel_id=channel_id, was_subscribed=bool(existing)
    )

    return (await _channel_reads(session, redis, user, [channel]))[0]
