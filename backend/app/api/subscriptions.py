from datetime import date, datetime, timezone

from fastapi import APIRouter, Form
from sqlalchemy import select

from app.core import mollie
from app.core.config import settings
from app.core.errors import api_error
from app.deps.db import CurrentAsyncSession
from app.deps.users import CurrentVerifiedUser
from app.models.supporter_subscription import SupporterSubscription
from app.models.user_subscription import UserSubscription
from app.schemas.subscription import SupporterCheckoutResult, SupporterSubscriptionRead

router = APIRouter(prefix="/subscriptions")


def _require_enabled():
    if not settings.SUBSCRIPTIONS_ENABLED:
        raise api_error(404, "subscriptions_disabled")


def _mollie_date_to_datetime(value: str | None) -> datetime | None:
    """Mollie's `nextPaymentDate` is a plain `YYYY-MM-DD` date, not a timestamp."""
    if not value:
        return None
    return datetime.combine(
        date.fromisoformat(value), datetime.min.time(), tzinfo=timezone.utc
    )


def _webhook_url() -> str:
    return f"{settings.PUBLIC_BASE_URL}{settings.API_PATH}/subscriptions/webhook/mollie"


async def _grant_supporter(session: CurrentAsyncSession, user_id) -> None:
    """Stages the entitlement row — does NOT commit. Always call this in the same
    transaction as the `SupporterSubscription` write it accompanies (one
    `session.commit()` for both), so the two tables can never land out of sync
    (e.g. a payment marked "active" but the supporter perk never granted because
    the process died between two separate commits)."""
    existing = await session.scalar(
        select(UserSubscription).where(
            UserSubscription.user_id == user_id, UserSubscription.kind == "supporter"
        )
    )
    if not existing:
        session.add(UserSubscription(user_id=user_id, kind="supporter"))


async def _revoke_supporter(session: CurrentAsyncSession, user_id) -> None:
    """Stages the entitlement removal — does NOT commit; see `_grant_supporter`."""
    existing = await session.scalar(
        select(UserSubscription).where(
            UserSubscription.user_id == user_id, UserSubscription.kind == "supporter"
        )
    )
    if existing:
        await session.delete(existing)


@router.get("/supporter", response_model=SupporterSubscriptionRead)
async def get_supporter_subscription(
    session: CurrentAsyncSession, user: CurrentVerifiedUser
):
    """Current state of the caller's supporter subscription, for a "manage
    subscription" screen. Reads our own DB only — never calls Mollie — so it works
    even with `SUBSCRIPTIONS_ENABLED` off (a user who already became a supporter
    before the feature was toggled off keeps seeing accurate status)."""
    sub = await session.scalar(
        select(SupporterSubscription).where(SupporterSubscription.user_id == user.id)
    )
    if not sub:
        return SupporterSubscriptionRead(
            active=False, status="none", current_period_end=None
        )
    return SupporterSubscriptionRead(
        active=sub.status == "active",
        status=sub.status,
        current_period_end=sub.current_period_end,
    )


@router.post("/supporter/checkout", response_model=SupporterCheckoutResult)
async def create_supporter_checkout(
    session: CurrentAsyncSession, user: CurrentVerifiedUser
):
    """Starts the recurring-payment flow (see app/core/mollie.py): creates a Mollie
    customer for this user if needed, then a first payment that establishes the
    mandate the actual Subscription resource will later be charged against. The
    Subscription itself is only created once that first payment is confirmed paid —
    see `mollie_webhook` below.

    Returns a hosted Mollie checkout URL; the client opens it in a browser (MVP is
    web-checkout only, see CLAUDE.md's supporter-subscription note — native in-app
    purchase is a later addition)."""
    _require_enabled()

    existing = await session.scalar(
        select(SupporterSubscription).where(SupporterSubscription.user_id == user.id)
    )
    if existing and existing.status == "active":
        raise api_error(409, "already_supporter")

    if existing:
        customer_id = existing.mollie_customer_id
    else:
        customer = await mollie.create_customer(email=user.email, name=user.username)
        customer_id = customer["id"]

    payment = await mollie.create_first_payment(
        customer_id=customer_id,
        amount=settings.SUPPORTER_PRICE_AMOUNT,
        currency=settings.SUPPORTER_PRICE_CURRENCY,
        description="Poulse Kora supporter subscription",
        redirect_url=settings.SUBSCRIPTION_CHECKOUT_REDIRECT_URL,
        webhook_url=_webhook_url(),
        metadata={"user_id": str(user.id)},
    )

    if existing:
        existing.mollie_customer_id = customer_id
        existing.mollie_subscription_id = None
        existing.status = "pending_first_payment"
        existing.current_period_end = None
    else:
        session.add(
            SupporterSubscription(
                user_id=user.id,
                mollie_customer_id=customer_id,
                status="pending_first_payment",
            )
        )
    await session.commit()

    return SupporterCheckoutResult(checkout_url=payment["_links"]["checkout"]["href"])


@router.post("/supporter/cancel", status_code=204)
async def cancel_supporter_subscription(
    session: CurrentAsyncSession, user: CurrentVerifiedUser
):
    """Cancels at Mollie and revokes the `supporter` entitlement immediately.

    MVP simplification: no "active until the period you already paid for ends" grace
    period — cancelling takes effect right away, matching `UserSubscription`'s
    existing all-or-nothing semantics (see its docstring). Worth revisiting if
    supporters complain about losing the days they already paid for.
    """
    _require_enabled()

    sub = await session.scalar(
        select(SupporterSubscription).where(SupporterSubscription.user_id == user.id)
    )
    if not sub or sub.status != "active":
        raise api_error(404, "not_a_supporter")

    if sub.mollie_subscription_id:
        await mollie.cancel_subscription(
            sub.mollie_customer_id, sub.mollie_subscription_id
        )

    sub.status = "canceled"
    await _revoke_supporter(session, user.id)
    await session.commit()


@router.post("/webhook/mollie", include_in_schema=False)
async def mollie_webhook(session: CurrentAsyncSession, id: str = Form(...)):
    """Mollie calls this for every payment belonging to a subscription (both the
    first, mandate-establishing one and every later renewal) — see
    https://docs.mollie.com/reference/webhooks. The body only ever carries a
    payment `id`; we always re-fetch the payment from Mollie's API rather than
    trust anything in the request, which is also what makes this endpoint safe to
    leave unauthenticated (a forged POST can at most make us re-fetch a real
    payment — it can't fabricate one).

    Deliberately not gated by `SUBSCRIPTIONS_ENABLED`: a subscription already in
    flight when the flag flips off should still get its renewals/cancellations
    processed rather than silently stop syncing.

    Always answers 200 (even "we don't recognize this payment") — a non-2xx makes
    Mollie retry the same webhook indefinitely, which would never help here.
    """
    payment = await mollie.get_payment(id)
    customer_id = payment.get("customerId")
    sub = await session.scalar(
        select(SupporterSubscription).where(
            SupporterSubscription.mollie_customer_id == customer_id
        )
    )
    if not sub:
        return {"ok": True}

    status_ = payment.get("status")
    mollie_subscription_id = payment.get("subscriptionId")

    if status_ == "paid" and sub.mollie_subscription_id is None:
        # First payment for this customer just succeeded: the mandate now exists,
        # so create the actual recurring Subscription resource at Mollie.
        subscription = await mollie.create_subscription(
            customer_id=sub.mollie_customer_id,
            amount=settings.SUPPORTER_PRICE_AMOUNT,
            currency=settings.SUPPORTER_PRICE_CURRENCY,
            interval=settings.SUPPORTER_INTERVAL,
            description="Poulse Kora supporter subscription",
            webhook_url=_webhook_url(),
            metadata={"user_id": str(sub.user_id)},
        )
        sub.mollie_subscription_id = subscription["id"]
        sub.status = "active"
        sub.current_period_end = _mollie_date_to_datetime(
            subscription.get("nextPaymentDate")
        )
        await _grant_supporter(session, sub.user_id)
        await session.commit()

    elif status_ == "paid" and mollie_subscription_id == sub.mollie_subscription_id:
        # A renewal payment succeeded — refresh the period end and make sure the
        # entitlement is (still) granted.
        subscription = await mollie.get_subscription(
            sub.mollie_customer_id, sub.mollie_subscription_id
        )
        if subscription.get("status") == "active":
            sub.status = "active"
        sub.current_period_end = _mollie_date_to_datetime(
            subscription.get("nextPaymentDate")
        )
        await _grant_supporter(session, sub.user_id)
        await session.commit()

    elif (
        status_ in ("failed", "expired", "canceled")
        and sub.mollie_subscription_id is None
    ):
        # The first, mandate-establishing payment never completed.
        sub.status = "failed"
        await session.commit()

    elif mollie_subscription_id == sub.mollie_subscription_id:
        # A renewal payment failed. Mollie retries automatically and only cancels
        # the subscription itself once retries are exhausted, so re-check the
        # subscription resource rather than assuming.
        subscription = await mollie.get_subscription(
            sub.mollie_customer_id, sub.mollie_subscription_id
        )
        if subscription.get("status") in ("canceled", "suspended", "completed"):
            sub.status = "canceled"
            await _revoke_supporter(session, sub.user_id)
            await session.commit()

    return {"ok": True}
