from datetime import datetime
from typing import Literal

from pydantic import BaseModel

SubscriptionKind = Literal["free", "supporter"]

#: Mirrors `SupporterSubscription.status` (app/models/supporter_subscription.py),
#: plus "none" for a user who has never started a checkout.
SupporterStatus = Literal[
    "none", "pending_first_payment", "active", "canceled", "failed"
]


class SupporterSubscriptionRead(BaseModel):
    """`GET /subscriptions/supporter` — current state of the caller's supporter
    subscription. `active` is the one field the mobile app actually needs to gate
    UI on; `status`/`current_period_end` are there for a "manage subscription"
    screen."""

    active: bool
    status: SupporterStatus
    current_period_end: datetime | None


class SupporterCheckoutResult(BaseModel):
    """`POST /subscriptions/supporter/checkout` — open this URL (a Mollie-hosted
    payment page) in a browser/webview to complete the first payment."""

    checkout_url: str
