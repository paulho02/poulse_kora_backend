from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.deps.db import CurrentAsyncSession
from app.deps.redis import CurrentRedis
from app.deps.users import CurrentUser
from app.feed import service
from app.models.channel import Channel
from app.models.channel_subscription import ChannelSubscription
from app.schemas.channel import ChannelRead

router = APIRouter(prefix="/channels")


async def _subscribed_channel_ids(session: CurrentAsyncSession, user_id) -> set[int]:
    result = await session.execute(
        select(ChannelSubscription.channel_id).filter(
            ChannelSubscription.user_id == user_id
        )
    )
    return set(result.scalars().all())


def _to_read(channel: Channel, subscribed_ids: set[int]) -> ChannelRead:
    return ChannelRead(
        id=channel.id,
        name=channel.name,
        color=channel.color,
        description=channel.description,
        is_subscribed=channel.id in subscribed_ids,
    )


@router.get("", response_model=list[ChannelRead])
async def list_channels(
    session: CurrentAsyncSession,
    user: CurrentUser,
    q: str | None = None,
):
    query = select(Channel).order_by(Channel.name)
    if q:
        query = query.filter(
            Channel.name.ilike(f"%{q}%") | Channel.description.ilike(f"%{q}%")
        )
    channels = (await session.execute(query)).scalars().all()
    subscribed_ids = await _subscribed_channel_ids(session, user.id)
    return [_to_read(c, subscribed_ids) for c in channels]


@router.post("/{channel_id}/subscribe", response_model=ChannelRead)
async def subscribe_channel(
    channel_id: int,
    session: CurrentAsyncSession,
    user: CurrentUser,
    redis: CurrentRedis,
):
    channel = await session.get(Channel, channel_id)
    if not channel:
        raise HTTPException(404)

    existing = await session.scalar(
        select(ChannelSubscription).filter(
            ChannelSubscription.user_id == user.id,
            ChannelSubscription.channel_id == channel_id,
        )
    )
    if not existing:
        session.add(ChannelSubscription(user_id=user.id, channel_id=channel_id))
        await session.commit()

    # Mirror into Redis: add to the channel's subscriber set and make the user
    # reachable by fan-out (in free_queue if their queue has room).
    #
    # Deliberately no history backfill. A new subscriber's queue fills from the worker
    # alone: posts published from now on, plus any parked in ops:retry because the
    # channel had no free recipient — which is exactly the backlog case. Pulling
    # already-distributed history here would be a second delivery path racing those
    # retries, which is how the same post used to land in the queue twice.
    await service.sync_subscribe(redis, str(user.id), channel_id)

    subscribed_ids = await _subscribed_channel_ids(session, user.id)
    return _to_read(channel, subscribed_ids)


@router.post("/{channel_id}/unsubscribe", response_model=ChannelRead)
async def unsubscribe_channel(
    channel_id: int,
    session: CurrentAsyncSession,
    user: CurrentUser,
    redis: CurrentRedis,
):
    channel = await session.get(Channel, channel_id)
    if not channel:
        raise HTTPException(404)

    existing = await session.scalar(
        select(ChannelSubscription).filter(
            ChannelSubscription.user_id == user.id,
            ChannelSubscription.channel_id == channel_id,
        )
    )
    if existing:
        await session.delete(existing)
        await session.commit()

    # Mirror into Redis: remove the user from the channel's subscriber set so they
    # no longer receive fan-out from it. Already-queued posts are left in place.
    await service.sync_unsubscribe(redis, str(user.id), channel_id)

    subscribed_ids = await _subscribed_channel_ids(session, user.id)
    return _to_read(channel, subscribed_ids)
