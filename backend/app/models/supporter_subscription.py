from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi_users_db_sqlalchemy import GUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.schema import ForeignKey
from sqlalchemy.sql.sqltypes import DateTime, String

from app.db import Base

if TYPE_CHECKING:
    from app.models.user import User


class SupporterSubscription(Base):
    """The Mollie-side lifecycle of a user's "supporter" payment subscription.

    One row per user (at most one supporter subscription each). This is deliberately
    separate from `UserSubscription` — that table is the entitlement flag the rest of
    the app (e.g. `Post.subscription_kind` in app/api/posts.py) already reads, and
    knows nothing about payment providers. This table is the payment side: it tracks
    the Mollie customer/subscription IDs and lifecycle status needed to react to
    renewals, failures and cancellations. The webhook handler
    (app/api/subscriptions.py) is what keeps the two in sync — granting/revoking the
    `UserSubscription(kind="supporter")` row as this one's `status` changes.

    `status` values: "pending_first_payment" (checkout started, mandate not yet
    established), "active", "canceled", "failed" (first payment never completed).
    `mollie_subscription_id` is only set once the first payment succeeds and the
    actual recurring Subscription is created at Mollie.
    """

    __tablename__ = "supporter_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("users.id"), unique=True
    )

    mollie_customer_id: Mapped[str] = mapped_column(String(50))
    mollie_subscription_id: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24))
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship()
