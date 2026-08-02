from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import mollie
from app.core.config import settings
from app.models.supporter_subscription import SupporterSubscription
from app.models.user_subscription import UserSubscription
from tests.utils import generate_random_string, get_jwt_header, grant_subscription


@pytest.fixture(autouse=True)
def enable_subscriptions(monkeypatch):
    monkeypatch.setattr(settings, "SUBSCRIPTIONS_ENABLED", True)


async def _get_subscription(db: AsyncSession, user_id) -> SupporterSubscription | None:
    return await db.scalar(
        select(SupporterSubscription).where(SupporterSubscription.user_id == user_id)
    )


def _mollie_id(prefix: str) -> str:
    """A fake Mollie-style ID, unique per call. `db` and `create_user` are
    session-scoped and their commits persist in the test database across
    separate `pytest` invocations (unlike the per-function `auto_rollback`), so a
    fixed literal like "cst_1" would collide with a leftover row from a previous
    run — the webhook handler looks subscriptions up by `mollie_customer_id`,
    which is never actually reused across customers in real Mollie data."""
    return f"{prefix}_{generate_random_string(16)}"


class TestGetSupporterStatus:
    async def test_anonymous_rejected(self, client: AsyncClient):
        resp = await client.get(settings.API_PATH + "/subscriptions/supporter")
        assert resp.status_code == 401

    async def test_none_when_never_subscribed(self, client: AsyncClient, create_user):
        user = await create_user()
        resp = await client.get(
            settings.API_PATH + "/subscriptions/supporter", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "active": False,
            "status": "none",
            "current_period_end": None,
        }

    async def test_reflects_active_subscription(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        user = await create_user()
        db.add(
            SupporterSubscription(
                user_id=user.id, mollie_customer_id=_mollie_id("cst"), status="active"
            )
        )
        await db.commit()

        resp = await client.get(
            settings.API_PATH + "/subscriptions/supporter", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["active"] is True
        assert resp.json()["status"] == "active"

    async def test_works_even_when_feature_disabled(
        self, client: AsyncClient, create_user, monkeypatch
    ):
        monkeypatch.setattr(settings, "SUBSCRIPTIONS_ENABLED", False)
        user = await create_user()
        resp = await client.get(
            settings.API_PATH + "/subscriptions/supporter", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text


class TestCheckout:
    async def test_disabled_returns_404(
        self, client: AsyncClient, create_user, monkeypatch
    ):
        monkeypatch.setattr(settings, "SUBSCRIPTIONS_ENABLED", False)
        user = await create_user()
        resp = await client.post(
            settings.API_PATH + "/subscriptions/supporter/checkout",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "subscriptions_disabled"

    async def test_anonymous_rejected(self, client: AsyncClient):
        resp = await client.post(
            settings.API_PATH + "/subscriptions/supporter/checkout"
        )
        assert resp.status_code == 401

    async def test_creates_pending_subscription_and_returns_checkout_url(
        self, client: AsyncClient, db: AsyncSession, create_user, monkeypatch
    ):
        created_customer_id = _mollie_id("cst")

        async def fake_create_customer(email, name=None):
            return {"id": created_customer_id}

        async def fake_create_first_payment(**kwargs):
            return {"_links": {"checkout": {"href": "https://mollie.test/checkout/abc"}}}

        monkeypatch.setattr(mollie, "create_customer", fake_create_customer)
        monkeypatch.setattr(mollie, "create_first_payment", fake_create_first_payment)

        user = await create_user()
        resp = await client.post(
            settings.API_PATH + "/subscriptions/supporter/checkout",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"checkout_url": "https://mollie.test/checkout/abc"}

        sub = await _get_subscription(db, user.id)
        assert sub.status == "pending_first_payment"
        assert sub.mollie_customer_id == created_customer_id

    async def test_already_active_is_rejected(
        self, client: AsyncClient, db: AsyncSession, create_user
    ):
        user = await create_user()
        db.add(
            SupporterSubscription(
                user_id=user.id, mollie_customer_id=_mollie_id("cst"), status="active"
            )
        )
        await db.commit()

        resp = await client.post(
            settings.API_PATH + "/subscriptions/supporter/checkout",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "already_supporter"


class TestCancel:
    async def test_disabled_returns_404(
        self, client: AsyncClient, create_user, monkeypatch
    ):
        monkeypatch.setattr(settings, "SUBSCRIPTIONS_ENABLED", False)
        user = await create_user()
        resp = await client.post(
            settings.API_PATH + "/subscriptions/supporter/cancel",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "subscriptions_disabled"

    async def test_not_a_supporter_returns_404(self, client: AsyncClient, create_user):
        user = await create_user()
        resp = await client.post(
            settings.API_PATH + "/subscriptions/supporter/cancel",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "not_a_supporter"

    async def test_cancels_active_subscription(
        self, client: AsyncClient, db: AsyncSession, create_user, monkeypatch
    ):
        customer_id, subscription_id = _mollie_id("cst"), _mollie_id("sub")
        cancel_calls = []

        async def fake_cancel_subscription(customer_id, subscription_id):
            cancel_calls.append((customer_id, subscription_id))
            return {"status": "canceled"}

        monkeypatch.setattr(mollie, "cancel_subscription", fake_cancel_subscription)

        user = await create_user()
        db.add(
            SupporterSubscription(
                user_id=user.id,
                mollie_customer_id=customer_id,
                mollie_subscription_id=subscription_id,
                status="active",
            )
        )
        await grant_subscription(db, user, "supporter")

        resp = await client.post(
            settings.API_PATH + "/subscriptions/supporter/cancel",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 204, resp.text
        assert cancel_calls == [(customer_id, subscription_id)]

        sub = await _get_subscription(db, user.id)
        assert sub.status == "canceled"

        entitlement = await db.scalar(
            select(UserSubscription).where(
                UserSubscription.user_id == user.id,
                UserSubscription.kind == "supporter",
            )
        )
        assert entitlement is None


class TestMollieWebhook:
    async def test_first_payment_paid_activates_supporter(
        self, client: AsyncClient, db: AsyncSession, create_user, monkeypatch
    ):
        customer_id, payment_id, subscription_id = (
            _mollie_id("cst"),
            _mollie_id("tr"),
            _mollie_id("sub"),
        )
        user = await create_user()
        db.add(
            SupporterSubscription(
                user_id=user.id,
                mollie_customer_id=customer_id,
                status="pending_first_payment",
            )
        )
        await db.commit()

        async def fake_get_payment(payment_id_arg):
            assert payment_id_arg == payment_id
            return {
                "id": payment_id,
                "status": "paid",
                "customerId": customer_id,
                "subscriptionId": None,
            }

        async def fake_create_subscription(**kwargs):
            return {
                "id": subscription_id,
                "status": "active",
                "nextPaymentDate": "2026-09-02",
            }

        monkeypatch.setattr(mollie, "get_payment", fake_get_payment)
        monkeypatch.setattr(mollie, "create_subscription", fake_create_subscription)

        resp = await client.post(
            settings.API_PATH + "/subscriptions/webhook/mollie", data={"id": payment_id}
        )
        assert resp.status_code == 200, resp.text

        sub = await _get_subscription(db, user.id)
        assert sub.status == "active"
        assert sub.mollie_subscription_id == subscription_id
        assert sub.current_period_end == datetime(2026, 9, 2, tzinfo=timezone.utc)

        entitlement = await db.scalar(
            select(UserSubscription).where(
                UserSubscription.user_id == user.id,
                UserSubscription.kind == "supporter",
            )
        )
        assert entitlement is not None

    async def test_renewal_payment_paid_refreshes_period_end(
        self, client: AsyncClient, db: AsyncSession, create_user, monkeypatch
    ):
        customer_id, payment_id, subscription_id = (
            _mollie_id("cst"),
            _mollie_id("tr"),
            _mollie_id("sub"),
        )
        user = await create_user()
        db.add(
            SupporterSubscription(
                user_id=user.id,
                mollie_customer_id=customer_id,
                mollie_subscription_id=subscription_id,
                status="active",
                current_period_end=datetime(2026, 8, 2, tzinfo=timezone.utc),
            )
        )
        await db.commit()

        async def fake_get_payment(payment_id_arg):
            return {
                "id": payment_id,
                "status": "paid",
                "customerId": customer_id,
                "subscriptionId": subscription_id,
            }

        async def fake_get_subscription(customer_id_arg, subscription_id_arg):
            return {"status": "active", "nextPaymentDate": "2026-10-02"}

        monkeypatch.setattr(mollie, "get_payment", fake_get_payment)
        monkeypatch.setattr(mollie, "get_subscription", fake_get_subscription)

        resp = await client.post(
            settings.API_PATH + "/subscriptions/webhook/mollie", data={"id": payment_id}
        )
        assert resp.status_code == 200, resp.text

        sub = await _get_subscription(db, user.id)
        assert sub.current_period_end == datetime(2026, 10, 2, tzinfo=timezone.utc)

    async def test_first_payment_failed_marks_failed(
        self, client: AsyncClient, db: AsyncSession, create_user, monkeypatch
    ):
        customer_id, payment_id = _mollie_id("cst"), _mollie_id("tr")
        user = await create_user()
        db.add(
            SupporterSubscription(
                user_id=user.id,
                mollie_customer_id=customer_id,
                status="pending_first_payment",
            )
        )
        await db.commit()

        async def fake_get_payment(payment_id_arg):
            return {
                "id": payment_id,
                "status": "failed",
                "customerId": customer_id,
                "subscriptionId": None,
            }

        monkeypatch.setattr(mollie, "get_payment", fake_get_payment)

        resp = await client.post(
            settings.API_PATH + "/subscriptions/webhook/mollie", data={"id": payment_id}
        )
        assert resp.status_code == 200, resp.text

        sub = await _get_subscription(db, user.id)
        assert sub.status == "failed"

        entitlement = await db.scalar(
            select(UserSubscription).where(
                UserSubscription.user_id == user.id,
                UserSubscription.kind == "supporter",
            )
        )
        assert entitlement is None

    async def test_renewal_failed_after_mollie_cancels_revokes_supporter(
        self, client: AsyncClient, db: AsyncSession, create_user, monkeypatch
    ):
        customer_id, payment_id, subscription_id = (
            _mollie_id("cst"),
            _mollie_id("tr"),
            _mollie_id("sub"),
        )
        user = await create_user()
        db.add(
            SupporterSubscription(
                user_id=user.id,
                mollie_customer_id=customer_id,
                mollie_subscription_id=subscription_id,
                status="active",
            )
        )
        await grant_subscription(db, user, "supporter")

        async def fake_get_payment(payment_id_arg):
            return {
                "id": payment_id,
                "status": "failed",
                "customerId": customer_id,
                "subscriptionId": subscription_id,
            }

        async def fake_get_subscription(customer_id_arg, subscription_id_arg):
            return {"status": "canceled"}

        monkeypatch.setattr(mollie, "get_payment", fake_get_payment)
        monkeypatch.setattr(mollie, "get_subscription", fake_get_subscription)

        resp = await client.post(
            settings.API_PATH + "/subscriptions/webhook/mollie", data={"id": payment_id}
        )
        assert resp.status_code == 200, resp.text

        sub = await _get_subscription(db, user.id)
        assert sub.status == "canceled"

        entitlement = await db.scalar(
            select(UserSubscription).where(
                UserSubscription.user_id == user.id,
                UserSubscription.kind == "supporter",
            )
        )
        assert entitlement is None

    async def test_unknown_customer_is_ignored(self, client: AsyncClient, monkeypatch):
        payment_id = _mollie_id("tr")

        async def fake_get_payment(payment_id_arg):
            return {
                "id": payment_id,
                "status": "paid",
                "customerId": _mollie_id("cst"),
                "subscriptionId": None,
            }

        monkeypatch.setattr(mollie, "get_payment", fake_get_payment)

        resp = await client.post(
            settings.API_PATH + "/subscriptions/webhook/mollie", data={"id": payment_id}
        )
        assert resp.status_code == 200
