"""Grant or revoke a user's subscription (e.g. "supporter") directly in Postgres.

Manual override alongside the real payment flow (see app/api/subscriptions.py,
app/models/supporter_subscription.py) — handy for comping a supporter or fixing a
one-off support case without touching Mollie. Bypasses the API/JWT entirely, the
same way `seed_dev_data.py` talks to Postgres directly. Note this only touches the
`UserSubscription` entitlement row, not `SupporterSubscription` (the Mollie-side
lifecycle) — a webhook for a manually-granted user would find no matching row and
just ignore it, which is the intended behavior here.

Usage (inside the backend container):
    docker compose exec backend python grant_subscription.py user@example.com supporter
    docker compose exec backend python grant_subscription.py user@example.com supporter --revoke
"""

import argparse
import asyncio

from sqlalchemy import select

from app.db import async_session_maker
from app.models.user import User
from app.models.user_subscription import UserSubscription
from app.schemas.subscription import SubscriptionKind


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="Email of the user to grant/revoke the subscription for.")
    parser.add_argument("kind", help=f"Subscription kind, one of: {SubscriptionKind}")
    parser.add_argument("--revoke", action="store_true", help="Remove the subscription instead of granting it.")
    args = parser.parse_args()

    async with async_session_maker() as session:
        user = await session.scalar(select(User).filter(User.email == args.email))
        if not user:
            parser.error(f"no user found with email {args.email!r}")

        existing = await session.scalar(
            select(UserSubscription).filter(
                UserSubscription.user_id == user.id,
                UserSubscription.kind == args.kind,
            )
        )

        if args.revoke:
            if not existing:
                print(f"{args.email} does not have subscription {args.kind!r}; nothing to revoke.")
                return
            await session.delete(existing)
            await session.commit()
            print(f"Revoked {args.kind!r} from {args.email}.")
            return

        if existing:
            print(f"{args.email} already has subscription {args.kind!r}; nothing to do.")
            return
        session.add(UserSubscription(user_id=user.id, kind=args.kind))
        await session.commit()
        print(f"Granted {args.kind!r} to {args.email}.")


if __name__ == "__main__":
    asyncio.run(main())
