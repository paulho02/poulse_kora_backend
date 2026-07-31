from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi_users_db_sqlalchemy import GUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.schema import ForeignKey, UniqueConstraint
from sqlalchemy.sql.sqltypes import DateTime, String

from app.db import Base

if TYPE_CHECKING:
    from app.models.user import User


class UserSubscription(Base):
    """One row per (user, subscription kind) the user currently holds.

    A user can hold several kinds at once (e.g. `supporter` plus some future
    `beta_tester`), but not the same kind twice. Ending a subscription means
    deleting its row here — there's no `revoked_at`/expiry, since nothing needs
    subscription *history*: `Post.subscription_kind` already snapshots the state
    permanently at creation time (see app/models/post.py).
    """

    __tablename__ = "user_subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", name="uq_user_subscription_user_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(GUID, ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(20))

    created: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="subscriptions")
